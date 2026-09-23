from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Callable


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    calls: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    open_until: float = 0.0
    probe_in_flight: bool = False


class CircuitBreaker:
    def __init__(
        self,
        provider_names: tuple[str, ...],
        *,
        clock: Callable[[], float] = monotonic,
        window_size: int = 20,
        minimum_calls: int = 5,
        failure_threshold: float = 0.5,
        open_seconds: float = 15.0,
    ) -> None:
        self._clock = clock
        self._window_size = window_size
        self._minimum_calls = minimum_calls
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._circuits = {
            name: _Circuit(calls=deque(maxlen=window_size)) for name in provider_names
        }

    def state(self, provider_name: str) -> CircuitState:
        circuit = self._circuits[provider_name]
        if circuit.state is CircuitState.OPEN and self._clock() >= circuit.open_until:
            circuit.state = CircuitState.HALF_OPEN
            circuit.probe_in_flight = False
        return circuit.state

    def is_eligible(self, provider_name: str) -> bool:
        circuit = self._circuits[provider_name]
        state = self.state(provider_name)
        return state is CircuitState.CLOSED or (
            state is CircuitState.HALF_OPEN and not circuit.probe_in_flight
        )

    def allow(self, provider_name: str) -> bool:
        circuit = self._circuits[provider_name]
        state = self.state(provider_name)
        if state is CircuitState.OPEN:
            return False
        if state is CircuitState.HALF_OPEN:
            if circuit.probe_in_flight:
                return False
            circuit.probe_in_flight = True
        return True

    def record(self, provider_name: str, *, failed: bool) -> None:
        circuit = self._circuits[provider_name]
        if self.state(provider_name) is CircuitState.HALF_OPEN:
            circuit.probe_in_flight = False
            if failed:
                self._open(circuit)
            else:
                circuit.state = CircuitState.CLOSED
                circuit.calls.clear()
            return
        if circuit.state is CircuitState.OPEN:
            return
        circuit.calls.append(failed)
        if len(circuit.calls) < self._minimum_calls:
            return
        if sum(circuit.calls) / len(circuit.calls) >= self._failure_threshold:
            self._open(circuit)

    def _open(self, circuit: _Circuit) -> None:
        circuit.state = CircuitState.OPEN
        circuit.open_until = self._clock() + self._open_seconds
        circuit.probe_in_flight = False
