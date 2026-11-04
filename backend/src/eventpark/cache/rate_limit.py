"""Token-bucket rate limiting in Redis, atomic via a Lua script (docs/02-architecture.md §7).

Token bucket: a bucket holds up to `capacity` tokens and refills at `refill_per_second`. Each request takes one token;
no token = 429. It allows short bursts (a full bucket) while capping the sustained rate (the refill rate).

Why Lua: "read the bucket, compute, write it back" in Python is a read-then-write race (two API instances read the
same 1 token, both allow). Redis runs a Lua script as ONE atomic step: no other command runs in between.

Fails OPEN: if Redis is down, requests are allowed (and a warning is logged). Rate limiting protects us from abuse; it
must never be the reason a gate stops working. Capacity and money never depend on Redis.
"""

import math
import time
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger()

BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])          -- tokens per second
local now = tonumber(ARGV[3])           -- milliseconds
local cost = tonumber(ARGV[4])
local bucket = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) / 1000 * rate)
local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil((cost - tokens) / rate * 1000)
end
redis.call('HSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {allowed, retry_ms}
"""


@dataclass(frozen=True)
class Limit:
    name: str
    capacity: int  # burst size
    per_seconds: float  # the bucket refills completely in this time

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.per_seconds


# The limits (tuned for a real venue: a gate lane handles ~1 car per 8 s, so 20 requests/s is pure abuse/bugs).
LOGIN_PER_IP = Limit("login-ip", capacity=20, per_seconds=60)
LOGIN_PER_EMAIL = Limit("login-email", capacity=5, per_seconds=60)  # slows down password guessing on one account
BOOKING_PER_USER = Limit("booking-user", capacity=10, per_seconds=60)
GATE_PER_DEVICE = Limit("gate-device", capacity=40, per_seconds=2)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_seconds: int = 0


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._script = redis.register_script(BUCKET_SCRIPT)

    async def hit(self, limit: Limit, key: str, *, cost: int = 1) -> Decision:
        now_ms = int(time.time() * 1000)
        try:
            allowed, retry_ms = await self._script(
                keys=[f"rl:{limit.name}:{key}"], args=[limit.capacity, limit.refill_per_second, now_ms, cost]
            )
        except (RedisError, OSError) as exc:
            log.warning("rate_limit.fail_open", limit=limit.name, error=type(exc).__name__, metric="RedisFallbacks")
            return Decision(allowed=True)
        if allowed:
            return Decision(allowed=True)
        log.info("rate_limit.limited", limit=limit.name, metric="RateLimited")
        return Decision(allowed=False, retry_after_seconds=max(1, math.ceil(int(retry_ms) / 1000)))
