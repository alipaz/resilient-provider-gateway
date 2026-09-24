import asyncio
import json

import httpx
import pytest
import respx

import app.api.routes as api_routes
from app.main import app
from app.observability.metrics import provider_attempt_counter, provider_duration, request_counter
from app.providers.circuit_breaker import CircuitBreaker, CircuitState
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
async def test_process_enforces_overall_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    slow_client = DeadlineProviderClient()
    audit_events: list[dict] = []
    before_attempts = provider_attempt_counter.labels("provider_a", "timeout")._value.get()
    before_duration = provider_duration.labels("provider_a")._sum.get()
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
    assert app.state.circuit_breaker.state("provider_a") is CircuitState.OPEN
    assert (
        provider_attempt_counter.labels("provider_a", "timeout")._value.get() == before_attempts + 1
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
