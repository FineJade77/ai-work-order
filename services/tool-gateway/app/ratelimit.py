import logging
import os

import redis

from app.redis_client import redis_client

logger = logging.getLogger("tool_gateway.ratelimit")

CAPACITY = int(os.environ.get("TOOL_RATE_LIMIT_CAPACITY", "20"))
REFILL_PER_SEC = float(os.environ.get("TOOL_RATE_LIMIT_REFILL_PER_SEC", "5"))

# Read-refill-take-write runs atomically inside Redis, so every gateway replica shares one
# bucket per key with no lost updates. Time comes from Redis TIME rather than the caller,
# so clock skew between gateway pods can't mint or destroy tokens.
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2]) / 1000
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * refill_per_ms)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill_per_ms) + 1000)
return allowed
"""

_token_bucket = redis_client.register_script(_TOKEN_BUCKET_LUA)


def acquire(tenant_id: str, tool_name: str) -> bool:
    key = f"tool_gateway:ratelimit:{tenant_id}:{tool_name}"
    try:
        return bool(_token_bucket(keys=[key], args=[CAPACITY, REFILL_PER_SEC, 1]))
    except redis.RedisError:
        # Fail open: a Redis outage shouldn't take every employee's IT assistant down with it.
        # Downstream systems still have their own limits; this is the outer guard.
        logger.exception("rate limiter unavailable, allowing call tenant_id=%s tool=%s", tenant_id, tool_name)
        return True
