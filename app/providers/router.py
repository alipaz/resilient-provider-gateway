from app.providers.models import ProviderConfig


class ProviderRouter:
    def __init__(self, configs: tuple[ProviderConfig, ProviderConfig, ProviderConfig]) -> None:
        expected_names = {"provider_a", "provider_b", "provider_c"}
        if len(configs) != 3 or {config.name for config in configs} != expected_names:
            raise ValueError("ProviderRouter requires providers A, B, and C")
        self._configs = {config.name: config for config in configs}
        self._order = tuple(config.name for config in configs)
        self._in_flight = {name: 0 for name in self._order}
        self._ewma_latency_ms: dict[str, float | None] = {name: None for name in self._order}
        self._next_tie_index = 0

    def select(self, eligible: set[str] | None = None) -> ProviderConfig:
        eligible_names = set(self._order) if eligible is None else set(self._order) & eligible
        if not eligible_names:
            raise LookupError("No eligible providers")
        lowest_load = min(self._normalized_load(name) for name in eligible_names)
        tied = {name for name in eligible_names if self._normalized_load(name) == lowest_load}
        for offset in range(len(self._order)):
            index = (self._next_tie_index + offset) % len(self._order)
            name = self._order[index]
            if name in tied:
                self._next_tie_index = (index + 1) % len(self._order)
                return self._configs[name]
        raise RuntimeError("Unable to select a provider")

    def start_attempt(self, config: ProviderConfig) -> None:
        self._in_flight[config.name] += 1

    def finish_attempt(self, config: ProviderConfig, latency_ms: float) -> None:
        self._in_flight[config.name] = max(0, self._in_flight[config.name] - 1)
        previous = self._ewma_latency_ms[config.name]
        self._ewma_latency_ms[config.name] = (
            latency_ms if previous is None else (previous * 0.8) + (latency_ms * 0.2)
        )

    def in_flight(self, provider_name: str) -> int:
        return self._in_flight[provider_name]

    def ewma_latency_ms(self, provider_name: str) -> float | None:
        return self._ewma_latency_ms[provider_name]

    @property
    def provider_names(self) -> tuple[str, ...]:
        return self._order

    def _normalized_load(self, provider_name: str) -> float:
        latency_ms = self._ewma_latency_ms[provider_name] or 1.0
        return self._in_flight[provider_name] * latency_ms
