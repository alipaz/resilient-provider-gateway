import pytest

from app.providers.models import ProviderConfig
from app.providers.router import ProviderRouter


def provider_configs() -> tuple[ProviderConfig, ProviderConfig, ProviderConfig]:
    return (
        ProviderConfig(name="provider_a", url="https://a.test/v1/process"),
        ProviderConfig(name="provider_b", url="https://b.test/v1/process"),
        ProviderConfig(name="provider_c", url="https://c.test/v1/process"),
    )


def test_router_can_select_a_lower_weight_provider() -> None:
    providers = provider_configs()
    router = ProviderRouter(providers, random_value=lambda: 0.9)
    router.finish_attempt(providers[2], latency_ms=5000.0, error_sample=0.0)

    assert router.select().name == "provider_c"


def test_router_excludes_ineligible_providers() -> None:
    router = ProviderRouter(provider_configs(), random_value=lambda: 0.0)

    selected = router.select(eligible={"provider_b"})

    assert selected.name == "provider_b"


@pytest.mark.parametrize(
    ("random_value", "expected_provider"),
    [(0.0, "provider_a"), (0.5, "provider_b"), (0.8, "provider_c")],
)
def test_router_selects_across_cumulative_weight_boundaries(
    random_value: float, expected_provider: str
) -> None:
    providers = provider_configs()
    router = ProviderRouter(providers, random_value=lambda: random_value)
    router.finish_attempt(providers[1], latency_ms=5000.0, error_sample=0.0)
    router.finish_attempt(providers[2], latency_ms=5000.0, error_sample=0.0)

    assert router.select().name == expected_provider


def test_router_excludes_zero_weight_providers() -> None:
    providers = provider_configs()
    router = ProviderRouter(
        providers,
        max_concurrency={"provider_a": 1, "provider_b": 1, "provider_c": 1},
        random_value=lambda: 0.0,
    )
    router.start_attempt(providers[0])

    assert router.select().name == "provider_b"


def test_router_uses_last_positive_weight_provider_for_floating_point_fallback() -> None:
    router = ProviderRouter(provider_configs(), random_value=lambda: 1.0)

    assert router.select().name == "provider_c"


def test_router_tracks_ewma_latency_after_an_attempt() -> None:
    router = ProviderRouter(provider_configs())
    provider = router.select()
    router.start_attempt(provider)

    router.finish_attempt(provider, latency_ms=100.0, error_sample=0.0)

    assert router.in_flight(provider.name) == 0
    assert router.ewma_latency_ms(provider.name) == 100.0


def test_router_updates_error_ewma_with_weighted_sample() -> None:
    provider = provider_configs()[0]
    router = ProviderRouter(provider_configs())

    router.finish_attempt(provider, latency_ms=1000.0, error_sample=0.5)

    assert router.ewma_error_rate(provider.name) == 0.1


def test_failed_result_reduces_reliability_and_routing_weight() -> None:
    provider = provider_configs()[0]
    router = ProviderRouter(provider_configs())
    initial_weight = router.routing_weight(provider.name)

    router.finish_attempt(provider, latency_ms=1000.0, error_sample=1.0)

    assert router.ewma_error_rate(provider.name) == 0.2
    assert router.routing_weight(provider.name) < initial_weight


def test_success_gradually_recovers_error_ewma() -> None:
    provider = provider_configs()[0]
    router = ProviderRouter(provider_configs())
    router.finish_attempt(provider, latency_ms=1000.0, error_sample=1.0)

    router.finish_attempt(provider, latency_ms=1000.0, error_sample=0.0)

    assert router.ewma_error_rate(provider.name) == pytest.approx(0.16)


def test_timeout_has_larger_error_ewma_and_lower_weight_than_server_error() -> None:
    providers = provider_configs()
    router = ProviderRouter(providers)

    router.finish_attempt(providers[0], latency_ms=1000.0, error_sample=0.5)
    router.finish_attempt(providers[1], latency_ms=1000.0, error_sample=1.0)

    assert router.ewma_error_rate("provider_b") > router.ewma_error_rate("provider_a")
    assert router.routing_weight("provider_b") < router.routing_weight("provider_a")


def test_router_squares_reliability_score_in_routing_weight() -> None:
    provider = provider_configs()[0]
    router = ProviderRouter(provider_configs())

    router.finish_attempt(provider, latency_ms=1000.0, error_sample=0.5)

    reliability_score = (1.0 - 0.1) ** 2
    assert router.routing_weight(provider.name) == pytest.approx(
        0.50 * reliability_score + 0.30 + 0.20
    )


def test_router_recovers_gradually_after_one_success() -> None:
    providers = provider_configs()
    router = ProviderRouter(providers)

    router.finish_attempt(providers[0], latency_ms=1000.0, error_sample=1.0)
    router.finish_attempt(providers[0], latency_ms=1000.0, error_sample=0.0)

    assert 0.0 < router.ewma_error_rate("provider_a") < 0.2
    assert router.routing_weight("provider_a") < router.routing_weight("provider_b")


def test_full_provider_capacity_has_zero_weight() -> None:
    provider = provider_configs()[0]
    router = ProviderRouter(
        provider_configs(),
        max_concurrency={"provider_a": 1, "provider_b": 1, "provider_c": 1},
    )
    router.start_attempt(provider)

    assert router.routing_weight(provider.name) == 0.0


def test_rate_limit_cooldown_sets_weight_to_zero_until_clock_advances() -> None:
    now = 100.0
    router = ProviderRouter(provider_configs(), clock=lambda: now)
    router.mark_rate_limited("provider_a", retry_after_seconds=5)

    assert router.is_rate_limited("provider_a") is True
    assert router.routing_weight("provider_a") == 0.0

    now = 105.0

    assert router.is_rate_limited("provider_a") is False
    assert router.select(eligible={"provider_a"}).name == "provider_a"


def test_all_zero_weights_raise_controlled_lookup_error() -> None:
    providers = provider_configs()
    router = ProviderRouter(
        providers,
        max_concurrency={"provider_a": 1, "provider_b": 1, "provider_c": 1},
    )
    for provider in providers:
        router.start_attempt(provider)

    with pytest.raises(LookupError, match="No provider with available capacity"):
        router.select()
