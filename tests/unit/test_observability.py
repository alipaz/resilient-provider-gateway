import asyncio
import json

import httpx
import pytest
import respx
import structlog

import app.api.routes as api_routes
import app.main as main_module
from app.core.context import set_request_context as real_set_request_context
from app.main import app
from app.observability.logging import configure_logging
from app.observability.metrics import (
    circuit_state,
    persistence_dispatch_failures,
    provider_attempt_counter,
    provider_duration,
    provider_inflight,
    rate_limit_rejections,
    request_counter,
)
from app.resilience.rate_limiter import RateLimitDecision


class RedisUnavailable:
    async def is_available(self) -> bool:
        return False


class AllowingRateLimiter:
    async def check(
        self,
        provider_name: str,
        capacity: int,
        refill_rate: float,
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)


@pytest.mark.asyncio
async def test_health_requests_propagate_isolated_correlation_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first, second = await asyncio.gather(
                client.get(
                    "/health/live",
                    headers={"X-Request-ID": "one", "X-Trace-ID": "trace-one"},
                ),
                client.get(
                    "/health/live",
                    headers={"X-Request-ID": "two", "X-Trace-ID": "trace-two"},
                ),
            )
    assert first.headers["x-request-id"] == "one"
    assert first.headers["x-trace-id"] == "trace-one"
    assert second.headers["x-request-id"] == "two"
    assert second.headers["x-trace-id"] == "trace-two"
    request_events = [
        event
        for event in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if event["event"] == "request_completed"
    ]
    assert {(event["request_id"], event["trace_id"]) for event in request_events} == {
        ("one", "trace-one"),
        ("two", "trace-two"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("header_name", ["X-Request-ID", "X-Trace-ID"])
async def test_invalid_correlation_header_returns_safe_422_and_is_observed(
    header_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_value = "invalid value!"
    before_requests = request_counter.labels("GET", "4xx")._value.get()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/live", headers={header_name: invalid_value})

    assert response.status_code == 422
    assert invalid_value not in response.text
    assert response.headers["x-request-id"] != invalid_value
    assert response.headers["x-trace-id"] != invalid_value
    assert len(response.headers["x-request-id"]) == 32
    assert len(response.headers["x-trace-id"]) == 32
    assert request_counter.labels("GET", "4xx")._value.get() == before_requests + 1
    request_event = next(
        event
        for event in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if event["event"] == "request_completed"
    )
    assert request_event["status_code"] == 422
    assert request_event["request_id"] == response.headers["x-request-id"]
    assert request_event["trace_id"] == response.headers["x-trace-id"]
    assert invalid_value not in json.dumps(request_event)


@pytest.mark.asyncio
async def test_readiness_fails_when_redis_is_unavailable() -> None:
    async with app.router.lifespan_context(app):
        app.state.rate_limiter = RedisUnavailable()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
    assert response.status_code == 503


def test_metrics_have_only_bounded_labels() -> None:
    assert request_counter._labelnames == ("method", "status_class")
    assert provider_attempt_counter._labelnames == ("provider", "outcome")
    assert provider_duration._labelnames == ("provider",)
    assert provider_inflight._labelnames == ("provider",)
    assert circuit_state._labelnames == ("provider",)
    assert rate_limit_rejections._labelnames == ("scope", "provider")
    assert persistence_dispatch_failures._labelnames == ()


def test_logging_renders_json() -> None:
    configure_logging()

    rendered = structlog.processors.JSONRenderer()(
        None,
        "info",
        {"request_id": "request-1", "trace_id": "trace-1", "event": "request_completed"},
    )

    assert json.loads(rendered)["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_process_establishes_correlation_context_once(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def record_context(request_id: str | None, trace_id: str | None) -> tuple[str, str]:
        calls.append((request_id, trace_id))
        return real_set_request_context(request_id, trace_id)

    monkeypatch.setattr(main_module, "set_request_context", record_context)
    monkeypatch.setattr(api_routes, "set_request_context", record_context, raising=False)
    monkeypatch.setattr(api_routes, "dispatch_audit_event", lambda event: True)
    respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = AllowingRateLimiter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/process",
                json={"value": "hello"},
                headers={"X-Request-ID": "request-once", "X-Trace-ID": "trace-once"},
            )

    assert response.status_code == 200
    assert calls == [("request-once", "trace-once")]


@pytest.mark.asyncio
async def test_process_records_provider_attempt_metrics(
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before_attempts = provider_attempt_counter.labels("provider_a", "success")._value.get()
    before_duration_sum = provider_duration.labels("provider_a")._sum.get()
    respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = AllowingRateLimiter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/process", json={"value": "hello"})

    assert response.status_code == 200
    assert (
        provider_attempt_counter.labels("provider_a", "success")._value.get() == before_attempts + 1
    )
    assert provider_duration.labels("provider_a")._sum.get() > before_duration_sum
    assert provider_inflight.labels("provider_a")._value.get() == 0
    log_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    provider_event = next(
        event for event in log_events if event["event"] == "provider_attempt_completed"
    )
    assert provider_event["provider"] == "provider_a"
    assert provider_event["attempt_number"] == 1
    assert provider_event["outcome"] == "success"
    assert provider_event["upstream_status"] == 200
    assert provider_event["request_id"]
    assert provider_event["trace_id"]


@pytest.mark.asyncio
async def test_trace_id_is_consistent_across_response_upstream_audit_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_events: list[dict] = []
    request_id = "request-correlation"
    trace_id = "trace-correlation"
    upstream = respx_mock.post("http://provider-a:8000/v1/process").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    monkeypatch.setattr(
        api_routes,
        "dispatch_audit_event",
        lambda event: audit_events.append(event),
    )

    async with app.router.lifespan_context(app):
        app.state.rate_limiter = AllowingRateLimiter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/process",
                json={"value": "hello"},
                headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
            )

    assert response.headers["x-trace-id"] == trace_id
    assert upstream.calls[0].request.headers["x-trace-id"] == trace_id
    assert audit_events[0]["request"]["trace_id"] == trace_id
    log_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    provider_event = next(
        event for event in log_events if event["event"] == "provider_attempt_completed"
    )
    request_event = next(event for event in log_events if event["event"] == "request_completed")
    assert provider_event["trace_id"] == trace_id
    assert request_event["trace_id"] == trace_id
