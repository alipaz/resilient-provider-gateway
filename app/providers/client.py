from time import perf_counter
from typing import Any

import httpx

from app.core.context import get_request_id, get_trace_id
from app.providers.models import ProviderAttempt, ProviderConfig, ProviderOutcome, ProviderResult


class ProviderClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def process(
        self,
        config: ProviderConfig,
        payload: dict[str, Any],
    ) -> ProviderResult:
        started_at = perf_counter()
        try:
            response = await self._http_client.post(
                config.url,
                json=payload,
                headers={
                    "X-Request-ID": get_request_id(),
                    "X-Trace-ID": get_trace_id(),
                },
            )
        except httpx.PoolTimeout:
            return self._result(config, started_at, None, ProviderOutcome.TIMEOUT, "pool_timeout")
        except httpx.ConnectTimeout:
            return self._result(
                config,
                started_at,
                None,
                ProviderOutcome.TIMEOUT,
                "connect_timeout",
            )
        except httpx.ReadTimeout:
            return self._result(config, started_at, None, ProviderOutcome.TIMEOUT, "read_timeout")
        except httpx.WriteTimeout:
            return self._result(config, started_at, None, ProviderOutcome.TIMEOUT, "write_timeout")
        except httpx.TimeoutException:
            return self._result(config, started_at, None, ProviderOutcome.TIMEOUT, "timeout")
        except httpx.ConnectError:
            return self._result(
                config,
                started_at,
                None,
                ProviderOutcome.CONNECTION_ERROR,
                "connection_error",
            )
        except httpx.RequestError:
            return self._result(
                config,
                started_at,
                None,
                ProviderOutcome.REQUEST_ERROR,
                "request_error",
            )

        response_json: dict[str, Any] | None = None
        try:
            parsed_json = response.json()
            if isinstance(parsed_json, dict):
                response_json = parsed_json
        except ValueError:
            pass
        if 200 <= response.status_code < 300 and response_json is None:
            return self._result(
                config,
                started_at,
                response.status_code,
                ProviderOutcome.REQUEST_ERROR,
                "invalid_json",
            )
        if 200 <= response.status_code < 300:
            return self._result(
                config,
                started_at,
                response.status_code,
                ProviderOutcome.SUCCESS,
                None,
                response_json,
            )
        if 400 <= response.status_code < 500:
            return self._result(
                config,
                started_at,
                response.status_code,
                ProviderOutcome.CLIENT_ERROR,
                "upstream_4xx",
                response_json,
                retry_after_seconds=(
                    self._retry_after_seconds(response.headers.get("Retry-After"))
                    if response.status_code == 429
                    else None
                ),
            )
        if 500 <= response.status_code < 600:
            return self._result(
                config,
                started_at,
                response.status_code,
                ProviderOutcome.SERVER_ERROR,
                "upstream_5xx",
                response_json,
            )
        return self._result(
            config,
            started_at,
            response.status_code,
            ProviderOutcome.REQUEST_ERROR,
            "unexpected_status",
        )

    @staticmethod
    def _result(
        config: ProviderConfig,
        started_at: float,
        status_code: int | None,
        outcome: ProviderOutcome,
        error_class: str | None,
        response_json: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            attempt=ProviderAttempt(
                provider_name=config.name,
                status_code=status_code,
                latency_ms=(perf_counter() - started_at) * 1000,
                outcome=outcome,
                error_class=error_class,
            ),
            response_json=response_json,
            retry_after_seconds=retry_after_seconds,
        )

    @staticmethod
    def _retry_after_seconds(value: str | None) -> int:
        """Parse Retry-After delta-seconds only; dates use the one-second default."""
        try:
            seconds = int(value) if value is not None else 0
        except ValueError:
            seconds = 0
        return min(60, seconds) if seconds > 0 else 1
