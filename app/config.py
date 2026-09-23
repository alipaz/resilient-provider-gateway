from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.models import ProviderConfig


class ProviderMode(StrEnum):
    MOCK = "mock"
    EXTERNAL = "external"


class Settings(BaseSettings):
    PROVIDER_MODE: ProviderMode = ProviderMode.MOCK
    PROVIDER_A_URL: str | None = None
    PROVIDER_B_URL: str | None = None
    PROVIDER_C_URL: str | None = None
    PROVIDER_CONNECT_TIMEOUT_SECONDS: float = 2.0
    PROVIDER_READ_TIMEOUT_SECONDS: float = 10.0
    PROVIDER_WRITE_TIMEOUT_SECONDS: float = 10.0
    PROVIDER_POOL_TIMEOUT_SECONDS: float = 2.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
