import httpx
import pytest
import respx
from pydantic import ValidationError

import app.main as main_module
from app.config import Settings
from app.core.context import get_request_id, get_trace_id, set_request_context
from app.providers.client import ProviderClient
from app.providers.models import ProviderConfig, ProviderOutcome


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client


def test_default_configuration_resolves_external_provider_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PROVIDER_A_URL", "PROVIDER_B_URL", "PROVIDER_C_URL"):
        monkeypatch.delenv(name, raising=False)

    configs = Settings(_env_file=None).provider_configs

    assert [config.url for config in configs] == [
        "http://provider.example.invalid:8001/v1/process",
        "http://provider.example.invalid:8002/v1/process",
        "http://provider.example.invalid:8003/v1/process",
    ]


def test_legacy_provider_mode_environment_does_not_change_external_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROVIDER_MODE", "mock")

    configs = Settings(_env_file=None).provider_configs

    assert [config.url for config in configs] == [
        "http://provider.example.invalid:8001/v1/process",
        "http://provider.example.invalid:8002/v1/process",
        "http://provider.example.invalid:8003/v1/process",
    ]


def test_explicit_provider_url_overrides_selected_mode() -> None:
    configs = Settings(
        _env_file=None,
        PROVIDER_A_URL="https://override.example/v1/process",
    ).provider_configs

    assert configs[0].url == "https://override.example/v1/process"
    assert configs[1].url == "http://provider.example.invalid:8002/v1/process"


def test_http_connection_limits_are_validated() -> None:
    settings = Settings(_env_file=None)

    assert settings.HTTP_MAX_CONNECTIONS == 200
    assert settings.HTTP_MAX_KEEPALIVE_CONNECTIONS == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, HTTP_MAX_CONNECTIONS=0)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            HTTP_MAX_CONNECTIONS=100,
            HTTP_MAX_KEEPALIVE_CONNECTIONS=101,
        )


@pytest.mark.asyncio
async def test_lifespan_passes_configured_http_connection_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.settings, "HTTP_MAX_CONNECTIONS", 37, raising=False)
    monkeypatch.setattr(main_module.settings, "HTTP_MAX_KEEPALIVE_CONNECTIONS", 19, raising=False)

    async with main_module.app.router.lifespan_context(main_module.app):
        pool = main_module.app.state.provider_client._http_client._transport._pool
        assert pool._max_connections == 37
        assert pool._max_keepalive_connections == 19


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
@pytest.mark.parametrize(
    ("retry_after", "expected_cooldown"),
    [
        ("12", 12),
        (None, 1),
        ("invalid", 1),
        ("0", 1),
        ("-1", 1),
        ("90", 60),
    ],
)
async def test_upstream_429_carries_bounded_delta_seconds_retry_after(
    http_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    retry_after: str | None,
    expected_cooldown: int,
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    respx_mock.post(config.url).mock(
        return_value=httpx.Response(429, headers=headers, json={"detail": "busy"})
    )

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.CLIENT_ERROR
    assert result.retry_after_seconds == expected_cooldown


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
@pytest.mark.parametrize(
    ("timeout", "expected_error_class"),
    [
        (httpx.PoolTimeout("pool timed out"), "pool_timeout"),
        (httpx.ConnectTimeout("connect timed out"), "connect_timeout"),
        (httpx.ReadTimeout("read timed out"), "read_timeout"),
        (httpx.WriteTimeout("write timed out"), "write_timeout"),
        (httpx.TimeoutException("timed out"), "timeout"),
    ],
)
async def test_timeout_subclasses_are_classified(
    http_client: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    timeout: httpx.TimeoutException,
    expected_error_class: str,
) -> None:
    config = ProviderConfig(name="provider_a", url="https://provider-a.test/v1/process")
    respx_mock.post(config.url).mock(side_effect=timeout)

    result = await ProviderClient(http_client).process(config, {"value": "hello"})

    assert result.outcome is ProviderOutcome.TIMEOUT
    assert result.status_code is None
    assert result.error_class == expected_error_class


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
