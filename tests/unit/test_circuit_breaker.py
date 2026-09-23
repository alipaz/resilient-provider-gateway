from app.providers.circuit_breaker import CircuitBreaker, CircuitState


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def open_circuit(breaker: CircuitBreaker) -> None:
    for failed in (True, True, True, False, False):
        breaker.record("provider_a", failed=failed)


def test_closed_circuit_opens_at_the_failure_threshold() -> None:
    clock = Clock()
    breaker = CircuitBreaker(("provider_a",), clock=clock)

    open_circuit(breaker)

    assert breaker.state("provider_a") is CircuitState.OPEN


def test_open_circuit_rejects_calls_before_the_open_timeout() -> None:
    clock = Clock()
    breaker = CircuitBreaker(("provider_a",), clock=clock)
    open_circuit(breaker)

    assert breaker.allow("provider_a") is False


def test_open_circuit_transitions_to_one_half_open_probe_after_timeout() -> None:
    clock = Clock()
    breaker = CircuitBreaker(("provider_a",), clock=clock)
    open_circuit(breaker)
    clock.advance(15)

    assert breaker.allow("provider_a") is True
    assert breaker.state("provider_a") is CircuitState.HALF_OPEN
    assert breaker.allow("provider_a") is False


def test_successful_half_open_probe_recovers_the_circuit() -> None:
    clock = Clock()
    breaker = CircuitBreaker(("provider_a",), clock=clock)
    open_circuit(breaker)
    clock.advance(15)
    assert breaker.allow("provider_a") is True

    breaker.record("provider_a", failed=False)

    assert breaker.state("provider_a") is CircuitState.CLOSED
    assert breaker.allow("provider_a") is True


def test_failed_half_open_probe_reopens_the_circuit() -> None:
    clock = Clock()
    breaker = CircuitBreaker(("provider_a",), clock=clock)
    open_circuit(breaker)
    clock.advance(15)
    assert breaker.allow("provider_a") is True

    breaker.record("provider_a", failed=True)

    assert breaker.state("provider_a") is CircuitState.OPEN
    assert breaker.allow("provider_a") is False
