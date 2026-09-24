import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest

from app.main import app
from app.providers.circuit_breaker import CircuitBreaker, CircuitState
from app.providers.client import ProviderClient
from app.resilience.admission import AdmissionController
from app.resilience.rate_limiter import RateLimitDecision
from mock_provider.main import MockProviderSettings, create_app

pytestmark = pytest.mark.integration


class AllowingRateLimiter:
    async def check(
        self,
        provider_name: str,
        capacity: int,
        refill_rate: float,
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)


class MockProviderTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        providers: dict[str, object],
        *,
        time_out: set[str] | None = None,
        block_provider: str | None = None,
    ) -> None:
        self.providers = providers
        self.time_out = time_out or set()
        self.block_provider = block_provider
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        provider = request.url.host
        self.calls.append(provider)
        if provider in self.time_out:
            raise httpx.ReadTimeout("mock timeout", request=request)
        if provider == self.block_provider:
            self.started.set()
            await self.release.wait()
        target = self.providers[provider]
        transport = httpx.ASGITransport(app=target)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{provider}") as client:
            response = await client.request(
                request.method,
                request.url.path,
                content=await request.aread(),
                headers=request.headers,
            )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )


def provider_app(
    name: str,
    *,
    failure_percentage: int = 0,
    rate_limit_after: int | None = None,
) -> object:
    return create_app(
        MockProviderSettings(
            PROVIDER_NAME=name,
            MOCK_LATENCY_MIN_MS=0,
            MOCK_LATENCY_MAX_MS=0,
            MOCK_5XX_PERCENT=failure_percentage,
            MOCK_RATE_LIMIT_AFTER=rate_limit_after,
            MOCK_SEED=1,
        )
    )


@asynccontextmanager
async def gateway_client(transport: MockProviderTransport) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        upstream_client = httpx.AsyncClient(transport=transport)
        app.state.provider_client = ProviderClient(upstream_client)
        app.state.rate_limiter = AllowingRateLimiter()
        gateway_transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=gateway_transport,
            base_url="http://gateway",
        ) as client:
            yield client
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_mock_provider_returns_deterministic_success_and_5xx() -> None:
    success = provider_app("provider_a")
    failure = provider_app("provider_b", failure_percentage=100)
    success_transport = httpx.ASGITransport(app=success)
    failure_transport = httpx.ASGITransport(app=failure)

    async with httpx.AsyncClient(transport=success_transport, base_url="http://provider") as client:
        success_response = await client.post("/v1/process", json={"value": "ok"})
    async with httpx.AsyncClient(transport=failure_transport, base_url="http://provider") as client:
        failure_response = await client.post("/v1/process", json={"value": "failed"})

    assert success_response.json() == {"provider": "provider_a", "status": "ok"}
    assert failure_response.status_code == 503


@pytest.mark.asyncio
async def test_gateway_successfully_proxies_to_default_mock_provider() -> None:
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a"),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        }
    )

    async with gateway_client(transport) as client:
        response = await client.post("/v1/process", json={"value": "ok"})

    assert response.status_code == 200
    assert response.json() == {"provider": "provider_a", "status": "ok"}
    assert transport.calls == ["provider-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["five_x", "timeout"])
async def test_gateway_fails_over_to_a_different_mock_provider(failure: str) -> None:
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a", failure_percentage=100),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        },
        time_out={"provider-a"} if failure == "timeout" else set(),
    )

    async with gateway_client(transport) as client:
        response = await client.post("/v1/process", json={"value": "retry"})

    assert response.status_code == 200
    assert response.headers["x-upstream-provider"] == "provider_b"
    assert transport.calls == ["provider-a", "provider-b"]


@pytest.mark.asyncio
async def test_upstream_429_falls_back_without_total_failure() -> None:
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a", rate_limit_after=0),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        }
    )

    async with gateway_client(transport) as client:
        response = await client.post("/v1/process", json={"value": "retry"})

    assert response.status_code == 200
    assert response.headers["x-upstream-provider"] == "provider_b"
    assert transport.calls == ["provider-a", "provider-b"]


@pytest.mark.asyncio
async def test_open_circuit_skips_degraded_mock_provider() -> None:
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a", failure_percentage=100),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        }
    )

    async with gateway_client(transport) as client:
        breaker = CircuitBreaker(
            app.state.provider_router.provider_names,
            minimum_calls=1,
            failure_threshold=1.0,
        )
        app.state.circuit_breaker = breaker
        first_response = await client.post("/v1/process", json={"value": "degraded"})
        assert breaker.state("provider_a") is CircuitState.OPEN
        response = await client.post("/v1/process", json={"value": "available"})

    assert first_response.status_code == 200
    assert first_response.headers["x-upstream-provider"] == "provider_b"
    assert response.status_code == 200
    assert response.headers["x-upstream-provider"] != "provider_a"
    assert transport.calls.count("provider-a") == 1


@pytest.mark.asyncio
async def test_global_backpressure_rejects_without_starting_more_provider_work() -> None:
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a"),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        },
        block_provider="provider-a",
    )

    async with gateway_client(transport) as client:
        app.state.admission = AdmissionController(max_inflight=1, wait_ms=1)
        first = asyncio.create_task(client.post("/v1/process", json={"value": "first"}))
        await transport.started.wait()
        rejected = await client.post("/v1/process", json={"value": "second"})
        transport.release.set()
        first_response = await first

    assert first_response.status_code == 200
    assert rejected.status_code == 429
    assert transport.calls == ["provider-a"]


@pytest.mark.asyncio
async def test_gateway_publishes_audit_event_with_request_and_attempt_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict] = []
    transport = MockProviderTransport(
        {
            "provider-a": provider_app("provider_a"),
            "provider-b": provider_app("provider_b"),
            "provider-c": provider_app("provider_c"),
        }
    )
    monkeypatch.setattr(
        "app.persistence.dispatch.persist_audit.apply_async",
        lambda *, args, queue, retry: published.append(args[0]),
    )

    async with gateway_client(transport) as client:
        response = await client.post(
            "/v1/process",
            json={"value": "audit"},
            headers={"X-Request-ID": "audit-request", "X-Trace-ID": "audit-trace"},
        )

    assert response.status_code == 200
    assert published[0]["request"]["trace_id"] == "audit-trace"
    assert published[0]["request"]["final_status"] == 200
    assert published[0]["attempts"] == [
        {
            "request_id": published[0]["request"]["id"],
            "provider": "provider_a",
            "attempt_number": 1,
            "started_at": published[0]["attempts"][0]["started_at"],
            "latency_ms": published[0]["attempts"][0]["latency_ms"],
            "http_status": 200,
            "outcome": "success",
            "error_class": None,
        }
    ]
