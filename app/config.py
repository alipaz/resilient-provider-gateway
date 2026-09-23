from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderMode(StrEnum):
    MOCK = "mock"
    EXTERNAL = "external"


class Settings(BaseSettings):
    PROVIDER_MODE: ProviderMode = ProviderMode.MOCK

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
