from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProviderOutcome(StrEnum):
    SUCCESS = "success"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    REQUEST_ERROR = "request_error"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider_name: str
    status_code: int | None
    latency_ms: float
    outcome: ProviderOutcome
    error_class: str | None


@dataclass(frozen=True, slots=True)
class ProviderResult:
    attempt: ProviderAttempt
    response_json: dict[str, Any] | None
    retry_after_seconds: int | None = None

    @property
    def provider_name(self) -> str:
        return self.attempt.provider_name

    @property
    def status_code(self) -> int | None:
        return self.attempt.status_code

    @property
    def latency_ms(self) -> float:
        return self.attempt.latency_ms

    @property
    def outcome(self) -> ProviderOutcome:
        return self.attempt.outcome

    @property
    def error_class(self) -> str | None:
        return self.attempt.error_class


@dataclass(frozen=True, slots=True)
class ProviderRequestResult:
    result: ProviderResult | None
    attempts: tuple[ProviderAttempt, ...]
