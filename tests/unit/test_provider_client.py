import httpx
import pytest
import respx

from app.config import ProviderMode, Settings
from app.core.context import get_request_id, get_trace_id, set_request_context
from app.providers.client import ProviderClient
from app.providers.models import ProviderConfig, ProviderOutcome


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client


def test_default_mode_resolves_docker_provider_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROVIDER_MODE", "PROVIDER_A_URL", "PROVIDER_B_URL", "PROVIDER_C_URL"):
        monkeypatch.delenv(name, raising=False)

    configs = Settings(_env_file=None).provider_configs

    assert [config.url for config in configs] == [
        "http://provider-a:8000/v1/process",
        "http://provider-b:8000/v1/process",
        "http://provider-c:8000/v1/process",
    ]


def test_external_mode_resolves_supplied_provider_urls() -> None:
    configs = Settings(_env_file=None, PROVIDER_MODE=ProviderMode.EXTERNAL).provider_configs

    assert [config.url for config in configs] == [
        "http://provider.example.invalid:8001/v1/process",
        "http://provider.example.invalid:8002/v1/process",
        "http://provider.example.invalid:8003/v1/process",
    ]


def test_explicit_provider_url_overrides_selected_mode() -> None:
    configs = Settings(
        _env_file=None,
        PROVIDER_MODE=ProviderMode.EXTERNAL,
        PROVIDER_A_URL="https://override.example/v1/process",
    ).provider_configs

    assert configs[0].url == "https://override.example/v1/process"
    assert configs[1].url == "http://provider.example.invalid:8002/v1/process"


@pytest.mark.asyncio
async def test_success_forwards_payload_and_trace_headers(
    http_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    route = respx_mock.post(config.url).mock(
        return_value=httpx.Response(200, json={"accepted": True})
    )
    set_request_context(request_id="request-123", trace_id="trace-456")

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.SUCCESS
    assert result.status_code == 200
    assert result.response_json == {"accepted": True}
    assert result.error_class is None
    assert result.latency_ms >= 0
    request = route.calls[0].request
    assert request.headers["x-request-id"] == "request-123"
    assert request.headers["x-trace-id"] == "trace-456"
    assert request.content == b'{"value":"hello"}'


@pytest.mark.asyncio
async def test_upstream_4xx_is_classified(
    http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    respx_mock.post(config.url).mock(return_value=httpx.Response(422, json={"detail": "invalid"}))

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.CLIENT_ERROR
    assert result.status_code == 422
    assert result.error_class == "upstream_4xx"
    assert result.response_json == {"detail": "invalid"}


@pytest.mark.asyncio
async def test_upstream_5xx_is_classified(
    http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    respx_mock.post(config.url).mock(return_value=httpx.Response(503))

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.SERVER_ERROR
    assert result.status_code == 503
    assert result.error_class == "upstream_5xx"


@pytest.mark.asyncio
async def test_timeout_is_classified(
    http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    respx_mock.post(config.url).mock(side_effect=httpx.ReadTimeout("read timed out"))

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.TIMEOUT
    assert result.status_code is None
    assert result.error_class == "timeout"


@pytest.mark.asyncio
async def test_connection_error_is_classified(
    http_client: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    respx_mock.post(config.url).mock(side_effect=httpx.ConnectError("connection failed"))

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.CONNECTION_ERROR
    assert result.status_code is None
    assert result.error_class == "connection_error"


def test_context_validates_and_generates_identifiers() -> None:
    request_id, trace_id = set_request_context(request_id=None, trace_id=None)

    assert get_request_id() == request_id
    assert get_trace_id() == trace_id
    assert len(request_id) == 32
    assert len(trace_id) == 32
    with pytest.raises(ValueError, match="request_id"):
        set_request_context(request_id="bad id", trace_id="trace-456")
