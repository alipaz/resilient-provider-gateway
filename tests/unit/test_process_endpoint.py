import httpx
import pytest
import respx

from app.main import app


class FailingProviderClient:
    async def process(self, config: object, payload: object, timeout: float | None = None) -> None:
        raise RuntimeError("unexpected client failure")


@pytest.fixture
async def api_client() -> httpx.AsyncClient:
    async with app.router.lifespan_context(app):
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
async def test_process_preserves_unexpected_client_error_and_releases_provider() -> None:
    async with app.router.lifespan_context(app):
        provider_router = app.state.provider_router
        app.state.provider_client = FailingProviderClient()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            caught_error: Exception | None = None
            try:
                await client.post("/v1/process", json={"value": "hello"})
            except Exception as error:
                caught_error = error

    assert (type(caught_error), provider_router.in_flight("provider_a")) == (RuntimeError, 0)


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
