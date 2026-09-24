import asyncio
import json

import httpx
import pytest
import respx

import app.api.routes as api_routes
import app.main as main_module
from app.main import app
from app.observability.metrics import (
    provider_attempt_counter,
    provider_duration,
    provider_timeout_counter,
    request_counter,
)
from app.providers.circuit_breaker import CircuitBreaker, CircuitState
from app.providers.models import ProviderAttempt, ProviderOutcome, ProviderResult
from app.providers.router import ProviderRouter
from app.resilience.admission import AdmissionController
from app.resilience.rate_limiter import RateLimitDecision, RateLimiterUnavailable


class FailingProviderClient:
    async def process(self, config: object, payload: object, timeout: float | None = None) -> None:
        raise RuntimeError("unexpected client failure")


class DeadlineProviderClient:
    def __init__(self) -> None:
        self.cancelled = False
        self._never = asyncio.Event()

    async def process(self, config: object, payload: object, timeout: float | None = None) -> None:
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeRateLimiter:
    def __init__(
        self,
        decisions: dict[str, bool] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.decisions = decisions or {}
        self.failure = failure

    async def check(
        self,
        provider_name: str,
        capacity: int,
        refill_rate: float,
    ) -> RateLimitDecision:
        if self.failure is not None:
            raise self.failure
        return RateLimitDecision(
            allowed=self.decisions.get(provider_name, True),
            retry_after_seconds=1,
        )


class PlannedRateLimiter:
    def __init__(
        self,
        decisions: dict[str, RateLimitDecision] | None = None,
        unavailable_for: set[str] | None = None,
    ) -> None:
        self.decisions = decisions or {}
        self.unavailable_for = unavailable_for or set()
        self.calls: list[str] = []

    async def check(
        self,
        provider_name: str,
        capacity: int,
        refill_rate: float,
    ) -> RateLimitDecision:
        self.calls.append(provider_name)
        if provider_name in self.unavailable_for:
            raise RateLimiterUnavailable()
        return self.decisions.get(provider_name, RateLimitDecision(True, 0))


@pytest.fixture(autouse=True)
def use_test_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "PROVIDER_A_URL", "http://provider-a:8000/v1/process")
    monkeypatch.setattr(main_module.settings, "PROVIDER_B_URL", "http://provider-b:8000/v1/process")
    monkeypatch.setattr(main_module.settings, "PROVIDER_C_URL", "http://provider-c:8000/v1/process")
    monkeypatch.setattr("app.providers.router.random", lambda: 0.0)


@pytest.fixture
async def api_client() -> httpx.AsyncClient:
    async with app.router.lifespan_context(app):
        app.state.rate_limiter = FakeRateLimiter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_process_forwards_headers_and_returns_upstream_status_and_body(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    upstream = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(422, json={"detail": "invalid input"})
    )

    response = await api_client.post(
        "/v1/process",
        json={"value": "hello"},
        headers={"X-Request-ID": "request-123", "X-Trace-ID": "trace-456"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid input"}
    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["x-trace-id"] == "trace-456"
    assert response.headers["x-upstream-provider"] == "provider_a"
    request = upstream.calls[0].request
    assert request.headers["x-request-id"] == "request-123"
    assert request.headers["x-trace-id"] == "trace-456"
    assert request.content == b'{"value":"hello"}'


@pytest.mark.asyncio
async def test_process_rejects_non_object_json(api_client: httpx.AsyncClient) -> None:
    response = await api_client.post("/v1/process", json=["not", "an", "object"])

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admission_rejection_publishes_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_events: list[dict] = []
    payload = {"value": "rejected"}
    monkeypatch.setattr(api_routes, "dispatch_audit_event", audit_events.append)

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = FakeRateLimiter()
        app.state.admission = AdmissionController(max_inflight=1, wait_ms=1)
        assert await app.state.admission.acquire() is True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/process",
                json=payload,
                headers={"X-Request-ID": "rejected-request", "X-Trace-ID": "rejected-trace"},
            )
        app.state.admission.release()

    assert response.status_code == 429
    assert response.json() == {"error": {"code": "admission_rejected"}}
    assert len(audit_events) == 1
    assert audit_events[0]["request"] == {
        "id": audit_events[0]["request"]["id"],
        "trace_id": "rejected-trace",
        "received_at": audit_events[0]["request"]["received_at"],
        "completed_at": audit_events[0]["request"]["completed_at"],
        "final_status": 429,
        "selected_provider": None,
        "total_latency_ms": audit_events[0]["request"]["total_latency_ms"],
        "payload_size": 20,
        "payload_sha256": "213de0db4ad47454032c47e69be5ee3e5e55d78ffccfe9d4c9fd31a2a97bfc80",
        "error_class": None,
    }
    assert audit_events[0]["attempts"] == []


@pytest.mark.asyncio
async def test_process_converts_non_json_upstream_response_to_bad_gateway(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "text/plain"},
        )
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_upstream_response"


@pytest.mark.asyncio
async def test_process_returns_controlled_error_for_unexpected_client_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_id = "unexpected-request"
    trace_id = "unexpected-trace"
    before_requests = request_counter.labels("POST", "5xx")._value.get()
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    async with app.router.lifespan_context(app):
        provider_router = app.state.provider_router
        app.state.provider_client = FailingProviderClient()
        app.state.rate_limiter = FakeRateLimiter()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/process",
                json={"value": "hello"},
                headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
            )

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error"}}
    assert "unexpected client failure" not in response.text
    assert response.headers["x-request-id"] == request_id
    assert response.headers["x-trace-id"] == trace_id
    assert provider_router.in_flight("provider_a") == 0
    assert request_counter.labels("POST", "5xx")._value.get() == before_requests + 1
    request_event = next(
        event
        for event in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if event["event"] == "request_completed"
    )
    assert request_event["status_code"] == 500
    assert request_event["request_id"] == request_id
    assert request_event["trace_id"] == trace_id


@pytest.mark.asyncio
async def test_unexpected_provider_exception_counts_as_router_ewma_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_unexpected(config: object, payload: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    async with app.router.lifespan_context(app):
        provider_router = app.state.provider_router
        monkeypatch.setattr(app.state.provider_client, "process", raise_unexpected)
        app.state.rate_limiter = FakeRateLimiter()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error"}}
    assert provider_router.in_flight("provider_a") == 0
    assert provider_router.ewma_error_rate("provider_a") == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("outcome", "status_code", "error_class", "expected_sample"),
    [
        (ProviderOutcome.SUCCESS, 200, None, 0.0),
        (ProviderOutcome.CLIENT_ERROR, 400, None, 0.0),
        (ProviderOutcome.CLIENT_ERROR, 429, None, 0.0),
        (ProviderOutcome.SERVER_ERROR, 503, None, 0.5),
        (ProviderOutcome.TIMEOUT, None, None, 1.0),
        (ProviderOutcome.CONNECTION_ERROR, None, None, 1.0),
        (ProviderOutcome.REQUEST_ERROR, None, "request_error", 1.0),
        (ProviderOutcome.SUCCESS, None, "request_error", 1.0),
    ],
)
def test_routing_error_sample_maps_provider_outcomes(
    outcome: ProviderOutcome,
    status_code: int | None,
    error_class: str | None,
    expected_sample: float,
) -> None:
    result = ProviderResult(
        attempt=ProviderAttempt(
            provider_name="provider_a",
            status_code=status_code,
            latency_ms=1.0,
            outcome=outcome,
            error_class=error_class,
        ),
        response_json=None,
    )

    assert api_routes._routing_error_sample(result) == expected_sample


@pytest.mark.asyncio
async def test_process_enforces_overall_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slow_client = DeadlineProviderClient()
    audit_events: list[dict] = []
    before_attempts = provider_attempt_counter.labels("provider_a", "timeout")._value.get()
    before_duration = provider_duration.labels("provider_a")._sum.get()
    before_timeouts = provider_timeout_counter.labels("provider_a", "deadline")._value.get()
    monkeypatch.setattr(api_routes, "dispatch_audit_event", audit_events.append)
    async with app.router.lifespan_context(app):
        provider_router = app.state.provider_router
        app.state.provider_client = slow_client
        app.state.rate_limiter = FakeRateLimiter()
        app.state.circuit_breaker = CircuitBreaker(
            provider_router.provider_names,
            minimum_calls=1,
            failure_threshold=1.0,
        )
        monkeypatch.setattr(app.state.settings, "REQUEST_DEADLINE_SECONDS", 0.01)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.post("/v1/process", json={"value": "hello"}),
                timeout=0.1,
            )

    assert response.status_code == 504
    assert response.json() == {"error": {"code": "upstream_timeout"}}
    assert response.headers["x-upstream-provider"] == "provider_a"
    assert slow_client.cancelled is True
    assert provider_router.in_flight("provider_a") == 0
    assert provider_router.ewma_error_rate("provider_a") == pytest.approx(0.2)
    assert app.state.circuit_breaker.state("provider_a") is CircuitState.OPEN
    assert (
        provider_attempt_counter.labels("provider_a", "timeout")._value.get() == before_attempts + 1
    )
    assert (
        provider_timeout_counter.labels("provider_a", "deadline")._value.get()
        == before_timeouts + 1
    )
    assert provider_duration.labels("provider_a")._sum.get() > before_duration
    assert len(audit_events) == 1
    assert audit_events[0]["request"]["selected_provider"] == "provider_a"
    assert len(audit_events[0]["attempts"]) == 1
    assert audit_events[0]["attempts"][0] == {
        "request_id": audit_events[0]["request"]["id"],
        "provider": "provider_a",
        "attempt_number": 1,
        "started_at": audit_events[0]["attempts"][0]["started_at"],
        "latency_ms": audit_events[0]["attempts"][0]["latency_ms"],
        "http_status": None,
        "outcome": "timeout",
        "error_class": "deadline_exceeded",
    }
    provider_event = next(
        event
        for event in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if event["event"] == "provider_attempt_completed"
    )
    assert provider_event["provider"] == "provider_a"
    assert provider_event["outcome"] == "timeout"
    assert provider_event["upstream_status"] is None
    assert provider_event["error_class"] == "deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "timeout_type"),
    [
        (httpx.PoolTimeout("pool timed out"), "pool"),
        (httpx.ConnectTimeout("connect timed out"), "connect"),
        (httpx.ReadTimeout("read timed out"), "read"),
        (httpx.WriteTimeout("write timed out"), "write"),
        (httpx.TimeoutException("timed out"), "other"),
    ],
)
async def test_process_records_each_timeout_phase_once(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    timeout: httpx.TimeoutException,
    timeout_type: str,
) -> None:
    before_timeouts = provider_timeout_counter.labels("provider_a", timeout_type)._value.get()
    respx_mock.post("http://provider-a:8000/v1/process").mock(side_effect=timeout)
    respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert (
        provider_timeout_counter.labels("provider_a", timeout_type)._value.get()
        == before_timeouts + 1
    )


@pytest.mark.asyncio
async def test_process_preserves_configured_httpx_phase_timeouts(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    upstream = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert upstream.calls[0].request.extensions["timeout"] == {
        "connect": 2.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 2.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_response", "first_error"),
    [
        (httpx.Response(429, json={"detail": "busy"}), None),
        (httpx.Response(503, json={"detail": "unavailable"}), None),
        (None, httpx.ReadTimeout("timed out")),
    ],
)
async def test_process_retries_retryable_failure_with_a_different_provider(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    first_response: httpx.Response | None,
    first_error: Exception | None,
) -> None:
    provider_a = respx_mock.post("http://provider-a:8000/v1/process")
    provider_a.mock(return_value=first_response, side_effect=first_error)
    provider_b = respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert response.json() == {"provider": "b"}
    assert response.headers["x-upstream-provider"] == "provider_b"
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 1


@pytest.mark.asyncio
async def test_upstream_429_cools_provider_without_reducing_reliability(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    now = 100.0
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "90"}, json={"detail": "busy"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    async with app.router.lifespan_context(app):
        app.state.provider_router = ProviderRouter(
            app.state.settings.provider_configs,
            max_concurrency={
                config.name: app.state.settings.provider_max_concurrent(config.name)
                for config in app.state.settings.provider_configs
            },
            clock=lambda: now,
            random_value=lambda: 0.0,
        )
        app.state.rate_limiter = FakeRateLimiter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

        provider_router = app.state.provider_router
        assert provider_router.routing_weight("provider_a") == 0.0
        assert provider_router.ewma_error_rate("provider_a") == 0.0
        now = 160.0
        assert provider_router.routing_weight("provider_a") > 0.0

    assert response.status_code == 200
    assert response.headers["x-upstream-provider"] == "provider_b"
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 1


@pytest.mark.asyncio
async def test_process_does_not_retry_a_non_retryable_4xx(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(400, json={"detail": "bad request"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process")

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 400
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 0


@pytest.mark.asyncio
async def test_process_stops_after_two_attempts(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    provider_c = respx_mock.post("http://provider-c:8000/v1/process")

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 1
    assert len(provider_c.calls) == 0


@pytest.mark.asyncio
async def test_process_returns_gateway_timeout_when_all_attempts_time_out(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post("http://provider-a:8000/v1/process").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    respx_mock.post("http://provider-b:8000/v1/process").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 504


@pytest.mark.asyncio
async def test_process_returns_service_unavailable_when_all_circuits_are_open(
    api_client: httpx.AsyncClient,
) -> None:
    breaker = app.state.circuit_breaker
    for provider_name in app.state.provider_router.provider_names:
        for _ in range(5):
            breaker.record(provider_name, failed=True)

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_provider_available"


@pytest.mark.asyncio
async def test_process_falls_back_after_a_provider_rate_limit_denial(
    api_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    app.state.rate_limiter = FakeRateLimiter({"provider_a": False, "provider_b": True})
    provider_b = respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert response.headers["x-upstream-provider"] == "provider_b"
    assert len(provider_b.calls) == 1


@pytest.mark.asyncio
async def test_process_marks_provider_cooldown_after_rate_limit_denial(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    limiter = PlannedRateLimiter(
        {
            "provider_a": RateLimitDecision(False, 7),
            "provider_b": RateLimitDecision(True, 0),
        }
    )
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = limiter
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

        assert app.state.provider_router.is_rate_limited("provider_a") is True

    assert response.status_code == 200
    assert limiter.calls == ["provider_a", "provider_b"]


@pytest.mark.asyncio
async def test_process_uses_retry_budget_before_second_upstream_attempt(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    limiter = PlannedRateLimiter()
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"provider": "b"})
    )

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = limiter
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 1
    assert limiter.calls == ["provider_a", "provider_b", "retry_budget"]


@pytest.mark.asyncio
async def test_process_returns_first_result_when_retry_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    limiter = PlannedRateLimiter({"retry_budget": RateLimitDecision(False, 1)})
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process")

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = limiter
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert response.headers["x-upstream-provider"] == "provider_a"
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 0
    assert limiter.calls == ["provider_a", "provider_b", "retry_budget"]


@pytest.mark.asyncio
async def test_process_returns_first_result_when_retry_budget_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    limiter = PlannedRateLimiter(unavailable_for={"retry_budget"})
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    provider_a = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    provider_b = respx_mock.post("http://provider-b:8000/v1/process")

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = limiter
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert response.headers["x-upstream-provider"] == "provider_a"
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 0
    assert limiter.calls == ["provider_a", "provider_b", "retry_budget"]


@pytest.mark.asyncio
async def test_process_maps_router_exhaustion_to_controlled_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    async with app.router.lifespan_context(app):
        for provider_name in app.state.provider_router.provider_names:
            app.state.provider_router.mark_rate_limited(provider_name, retry_after_seconds=10)
        app.state.rate_limiter = PlannedRateLimiter()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_provider_available"


@pytest.mark.asyncio
async def test_process_returns_too_many_requests_when_all_providers_are_rate_limited(
    api_client: httpx.AsyncClient,
) -> None:
    app.state.rate_limiter = FakeRateLimiter(
        {"provider_a": False, "provider_b": False, "provider_c": False}
    )

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_process_maps_rate_limiter_failure_to_service_unavailable(
    api_client: httpx.AsyncClient,
) -> None:
    app.state.rate_limiter = FakeRateLimiter(failure=RateLimiterUnavailable())

    response = await api_client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "rate_limiter_unavailable"
