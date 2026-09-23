from dataclasses import dataclass
from time import time
from typing import Protocol

TOKEN_BUCKET_LUA = """
-- token_bucket
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])
local values = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(values[1]) or capacity
local updated_at = tonumber(values[2]) or now_ms
tokens = math.min(capacity, tokens + ((now_ms - updated_at) / 1000) * refill_rate)
local allowed = tokens >= 1
if allowed then tokens = tokens - 1 end
redis.call('HMSET', key, 'tokens', tokens, 'updated_at', now_ms)
redis.call('PEXPIRE', key, math.ceil((capacity / refill_rate) * 2000))
local retry_after = 0
if not allowed then retry_after = math.ceil((1 - tokens) / refill_rate) end
return {allowed and 1 or 0, retry_after}
"""


class RedisEvaluator(Protocol):
    async def eval(self, *args: object) -> list[int | bytes]: ...


class RateLimiterUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class ProviderRateLimiter:
    def __init__(self, redis_client: RedisEvaluator) -> None:
        self._redis_client = redis_client

    async def check(
        self,
        provider_name: str,
        *,
        capacity: int,
        refill_rate: float,
    ) -> RateLimitDecision:
        try:
            result = await self._redis_client.eval(
                TOKEN_BUCKET_LUA,
                1,
                f"rate_limit:{provider_name}",
                int(time() * 1000),
                capacity,
                refill_rate,
            )
        except Exception as error:
            raise RateLimiterUnavailable() from error
        return RateLimitDecision(allowed=bool(int(result[0])), retry_after_seconds=int(result[1]))
