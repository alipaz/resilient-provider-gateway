from random import random
from time import monotonic
from typing import Callable

from app.providers.models import ProviderConfig

EWMA_ALPHA = 0.2
DEFAULT_TARGET_LATENCY_MS = 1000.0


class ProviderRouter:
    def __init__(
        self,
        configs: tuple[ProviderConfig, ProviderConfig, ProviderConfig],
        *,
        max_concurrency: dict[str, int] | None = None,
        target_latency_ms: float = DEFAULT_TARGET_LATENCY_MS,
        clock: Callable[[], float] = monotonic,
        random_value: Callable[[], float] | None = None,
    ) -> None:
        expected_names = {"provider_a", "provider_b", "provider_c"}
        if len(configs) != 3 or {config.name for config in configs} != expected_names:
            raise ValueError("ProviderRouter requires providers A, B, and C")
        self._configs = {config.name: config for config in configs}
        self._order = tuple(config.name for config in configs)
        self._in_flight = {name: 0 for name in self._order}
        self._max_concurrency = max_concurrency or {name: 1 for name in self._order}
        self._ewma_latency_ms: dict[str, float | None] = {name: None for name in self._order}
        self._ewma_error_rate = {name: 0.0 for name in self._order}
        self._rate_limited_until = {name: 0.0 for name in self._order}
        self._target_latency_ms = target_latency_ms
        self._clock = clock
        self._random_value = random if random_value is None else random_value

    def select(self, eligible: set[str] | None = None) -> ProviderConfig:
        eligible_names = set(self._order) if eligible is None else set(self._order) & eligible
        if not eligible_names:
            raise LookupError("No eligible providers")
        candidates: list[tuple[str, float]] = []
        for name in self._order:
            if name not in eligible_names:
                continue
            weight = self.routing_weight(name)
            if weight > 0.0:
                candidates.append((name, weight))
        if not candidates:
            raise LookupError("No provider with available capacity")
        total_weight = sum(weight for _, weight in candidates)
        threshold = self._random_value() * total_weight
        cumulative = 0.0
        for name, weight in candidates:
            cumulative += weight
            if threshold < cumulative:
                return self._configs[name]
        return self._configs[candidates[-1][0]]

    def start_attempt(self, config: ProviderConfig) -> None:
        self._in_flight[config.name] += 1

    def finish_attempt(
        self,
        config: ProviderConfig,
        latency_ms: float,
        *,
        error_sample: float,
    ) -> None:
        self._in_flight[config.name] = max(0, self._in_flight[config.name] - 1)
        previous = self._ewma_latency_ms[config.name]
        self._ewma_latency_ms[config.name] = (
            latency_ms
            if previous is None
            else (previous * (1 - EWMA_ALPHA)) + (latency_ms * EWMA_ALPHA)
        )
        error_sample = min(1.0, max(0.0, error_sample))
        self._ewma_error_rate[config.name] = (
            self._ewma_error_rate[config.name] * (1 - EWMA_ALPHA) + error_sample * EWMA_ALPHA
        )

    def in_flight(self, provider_name: str) -> int:
        return self._in_flight[provider_name]

    def ewma_latency_ms(self, provider_name: str) -> float | None:
        return self._ewma_latency_ms[provider_name]

    def ewma_error_rate(self, provider_name: str) -> float:
        return self._ewma_error_rate[provider_name]

    def mark_rate_limited(self, provider_name: str, retry_after_seconds: int) -> None:
        self._rate_limited_until[provider_name] = max(
            self._rate_limited_until[provider_name],
            self._clock() + retry_after_seconds,
        )

    def is_rate_limited(self, provider_name: str) -> bool:
        return self._clock() < self._rate_limited_until[provider_name]

    def routing_weight(self, provider_name: str) -> float:
        in_flight = self._in_flight[provider_name]
        max_concurrency = self._max_concurrency[provider_name]
        if in_flight >= max_concurrency or self.is_rate_limited(provider_name):
            return 0.0
        actual_latency = self._ewma_latency_ms[provider_name] or self._target_latency_ms
        latency_score = min(
            1.0,
            max(0.0, self._target_latency_ms / max(actual_latency, self._target_latency_ms)),
        )
        reliability_score = min(1.0, max(0.0, (1.0 - self._ewma_error_rate[provider_name]) ** 2))
        load_ratio = min(1.0, in_flight / max_concurrency)
        load_score = min(1.0, max(0.0, 1.0 - load_ratio))
        weight = 0.50 * reliability_score + 0.30 * latency_score + 0.20 * load_score
        return min(1.0, max(0.0, weight))

    @property
    def provider_names(self) -> tuple[str, ...]:
        return self._order
