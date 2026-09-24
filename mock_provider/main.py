import asyncio
from random import Random
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockProviderSettings(BaseSettings):
    PROVIDER_NAME: Literal["provider_a", "provider_b", "provider_c"] = "provider_a"
    MOCK_LATENCY_MIN_MS: int | None = Field(default=None, ge=0)
    MOCK_LATENCY_MAX_MS: int | None = Field(default=None, ge=0)
    MOCK_5XX_PERCENT: int = Field(default=0, ge=0, le=100)
    MOCK_TIMEOUT_MODE: bool = False
    MOCK_TIMEOUT_DELAY_MS: int = Field(default=30_000, ge=0)
    MOCK_RATE_LIMIT_AFTER: int | None = Field(default=None, ge=0)
    MOCK_SEED: int = 0

    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_latency(self) -> "MockProviderSettings":
        if self.latency_min_ms > self.latency_max_ms:
            raise ValueError("MOCK_LATENCY_MIN_MS must not exceed MOCK_LATENCY_MAX_MS")
        return self

    @property
    def latency_min_ms(self) -> int:
        if self.MOCK_LATENCY_MIN_MS is not None:
            return self.MOCK_LATENCY_MIN_MS
        return 300 if self.PROVIDER_NAME == "provider_a" else 1000

    @property
    def latency_max_ms(self) -> int:
        if self.MOCK_LATENCY_MAX_MS is not None:
            return self.MOCK_LATENCY_MAX_MS
        return 1000 if self.PROVIDER_NAME == "provider_a" else 10_000


class MockProvider:
    def __init__(self, settings: MockProviderSettings) -> None:
        self._settings = settings
        self._random = Random(settings.MOCK_SEED)
        self._request_count = 0

    async def process(self, payload: dict[str, Any]) -> JSONResponse:
        del payload
        self._request_count += 1
        latency_ms = self._random.randint(
            self._settings.latency_min_ms,
            self._settings.latency_max_ms,
        )
        await asyncio.sleep(latency_ms / 1000)
        if self._settings.MOCK_TIMEOUT_MODE:
            await asyncio.sleep(self._settings.MOCK_TIMEOUT_DELAY_MS / 1000)
        if (
            self._settings.MOCK_RATE_LIMIT_AFTER is not None
            and self._request_count > self._settings.MOCK_RATE_LIMIT_AFTER
        ):
            return JSONResponse(status_code=429, content={"error": "rate_limited"})
        if self._random.randrange(100) < self._settings.MOCK_5XX_PERCENT:
            return JSONResponse(status_code=503, content={"error": "unavailable"})
        return JSONResponse(
            status_code=200,
            content={"provider": self._settings.PROVIDER_NAME, "status": "ok"},
        )


def create_app(settings: MockProviderSettings | None = None) -> FastAPI:
    provider = MockProvider(settings or MockProviderSettings())
    app = FastAPI()

    @app.post("/v1/process")
    async def process(payload: dict[str, Any]) -> JSONResponse:
        return await provider.process(payload)

    return app


app = create_app()
