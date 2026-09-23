import pytest

from app.resilience.rate_limiter import ProviderRateLimiter, RateLimiterUnavailable


class FakeRedis:
    def __init__(self, result: list[int] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> list[int]:
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_token_bucket_decision_is_one_atomic_lua_evaluation() -> None:
    redis = FakeRedis([1, 0])
    limiter = ProviderRateLimiter(redis)

    decision = await limiter.check("provider_a", capacity=10, refill_rate=10)

    assert decision.allowed is True
    assert decision.retry_after_seconds == 0
    assert len(redis.calls) == 1
    assert redis.calls[0][1] == 1
    assert "token_bucket" in str(redis.calls[0][0])


@pytest.mark.asyncio
async def test_redis_error_becomes_rate_limiter_unavailable() -> None:
    limiter = ProviderRateLimiter(FakeRedis(ConnectionError("redis unavailable")))

    with pytest.raises(RateLimiterUnavailable):
        await limiter.check("provider_a", capacity=10, refill_rate=10)
