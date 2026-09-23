import httpx
import pytest
import respx

from app.main import app


class FailingProviderClient:
    async def process(self, config: object, payload: object) -> None:
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
