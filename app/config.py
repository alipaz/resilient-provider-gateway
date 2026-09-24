from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.models import ProviderConfig


class ProviderMode(StrEnum):
    MOCK = "mock"
    EXTERNAL = "external"


class Settings(BaseSettings):
    PROVIDER_MODE: ProviderMode = ProviderMode.EXTERNAL
    PROVIDER_A_URL: str | None = None
    PROVIDER_B_URL: str | None = None
    PROVIDER_C_URL: str | None = None
    PROVIDER_CONNECT_TIMEOUT_SECONDS: float = 2.0
    PROVIDER_READ_TIMEOUT_SECONDS: float = 10.0
    PROVIDER_WRITE_TIMEOUT_SECONDS: float = 10.0
    PROVIDER_POOL_TIMEOUT_SECONDS: float = 2.0
    HTTP_MAX_CONNECTIONS: int = Field(default=200, gt=0)
    HTTP_MAX_KEEPALIVE_CONNECTIONS: int = Field(default=100, gt=0)
    REQUEST_DEADLINE_SECONDS: float = 15.0
    MAX_INFLIGHT: int = 200
    ADMISSION_WAIT_MS: int = 50
    REDIS_URL: str = "redis://localhost:6379/0"
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672//"
    DATABASE_URL: str = "postgresql+psycopg://localhost/audit"
    PROVIDER_A_RATE_CAPACITY: int = 10
    PROVIDER_A_RATE_REFILL_RPS: float = 10.0
    PROVIDER_B_RATE_CAPACITY: int = 100
    PROVIDER_B_RATE_REFILL_RPS: float = 100.0
    PROVIDER_C_RATE_CAPACITY: int = 100
    PROVIDER_C_RATE_REFILL_RPS: float = 100.0
    PROVIDER_A_MAX_CONCURRENT: int = 10
    PROVIDER_B_MAX_CONCURRENT: int = 100
    PROVIDER_C_MAX_CONCURRENT: int = 100
    RETRY_BUDGET_CAPACITY: int = 10
    RETRY_BUDGET_REFILL_RPS: float = 10.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_http_connection_limits(self) -> Self:
        if self.HTTP_MAX_KEEPALIVE_CONNECTIONS > self.HTTP_MAX_CONNECTIONS:
            raise ValueError("HTTP_MAX_KEEPALIVE_CONNECTIONS cannot exceed HTTP_MAX_CONNECTIONS")
        return self

    @property
    def provider_configs(self) -> tuple[ProviderConfig, ProviderConfig, ProviderConfig]:
        urls = self._mode_urls()
        overrides = (self.PROVIDER_A_URL, self.PROVIDER_B_URL, self.PROVIDER_C_URL)
        names = ("provider_a", "provider_b", "provider_c")
        return tuple(
            ProviderConfig(name=name, url=override or url)
            for name, url, override in zip(names, urls, overrides, strict=True)
        )  # type: ignore[return-value]

    def _mode_urls(self) -> tuple[str, str, str]:
        if self.PROVIDER_MODE is ProviderMode.EXTERNAL:
            return (
                "http://provider.example.invalid:8001/v1/process",
                "http://provider.example.invalid:8002/v1/process",
                "http://provider.example.invalid:8003/v1/process",
            )
        return (
            "http://provider-a:8000/v1/process",
            "http://provider-b:8000/v1/process",
            "http://provider-c:8000/v1/process",
        )

    def provider_rate_limit(self, provider_name: str) -> tuple[int, float]:
        limits = {
            "provider_a": (self.PROVIDER_A_RATE_CAPACITY, self.PROVIDER_A_RATE_REFILL_RPS),
            "provider_b": (self.PROVIDER_B_RATE_CAPACITY, self.PROVIDER_B_RATE_REFILL_RPS),
            "provider_c": (self.PROVIDER_C_RATE_CAPACITY, self.PROVIDER_C_RATE_REFILL_RPS),
        }
        return limits[provider_name]

    def provider_max_concurrent(self, provider_name: str) -> int:
        limits = {
            "provider_a": self.PROVIDER_A_MAX_CONCURRENT,
            "provider_b": self.PROVIDER_B_MAX_CONCURRENT,
            "provider_c": self.PROVIDER_C_MAX_CONCURRENT,
        }
        return limits[provider_name]
