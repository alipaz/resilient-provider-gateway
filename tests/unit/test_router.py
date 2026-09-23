from app.providers.models import ProviderConfig
from app.providers.router import ProviderRouter


def provider_configs() -> tuple[ProviderConfig, ProviderConfig, ProviderConfig]:
    return (
        ProviderConfig(name="provider_a", url="https://a.test/v1/process"),
        ProviderConfig(name="provider_b", url="https://b.test/v1/process"),
        ProviderConfig(name="provider_c", url="https://c.test/v1/process"),
    )


def test_router_rotates_through_all_providers_when_loads_tie() -> None:
    router = ProviderRouter(provider_configs())

    selected = [router.select().name for _ in range(4)]

    assert selected == ["provider_a", "provider_b", "provider_c", "provider_a"]


def test_router_excludes_ineligible_providers() -> None:
    router = ProviderRouter(provider_configs())

    selected = router.select(eligible={"provider_b"})

    assert selected.name == "provider_b"


def test_router_prefers_the_lowest_in_flight_load() -> None:
    router = ProviderRouter(provider_configs())
    first = router.select()
    router.start_attempt(first)

    selected = router.select()

    assert first.name == "provider_a"
    assert selected.name == "provider_b"


def test_router_tracks_ewma_latency_after_an_attempt() -> None:
    router = ProviderRouter(provider_configs())
    provider = router.select()
    router.start_attempt(provider)

    router.finish_attempt(provider, latency_ms=100.0)

    assert router.in_flight(provider.name) == 0
    assert router.ewma_latency_ms(provider.name) == 100.0
