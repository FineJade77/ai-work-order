import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import ApiError
from app.metrics import RATE_LIMIT_REFUSALS

logger = logging.getLogger("app_service.rate_limit")


class MessageRateLimiter:
    """Caps how many messages one user may send, per minute and per day, since each message costs
    two or three model calls. Fixed windows in Redis: cheap, and a burst of up to twice the minute
    limit across a window boundary is acceptable for a cost guard.

    Every attempt counts, including ones later refused for another reason (e.g. TURN_IN_PROGRESS),
    so a client retrying in a loop is throttled too.
    """

    def __init__(self, redis: Redis, *, per_minute: int, per_day: int, timezone: str):
        self._redis = redis
        self._per_minute = per_minute
        self._per_day = per_day
        self._tz = ZoneInfo(timezone)

    async def check(self, user_id: uuid.UUID, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, self._tz)
        next_minute = (math.floor(now / 60) + 1) * 60
        # Add a day to the date, not 24 hours to the instant, so a DST change can't shift midnight.
        next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        minute_key = f"ratelimit:msg:{user_id}:m:{math.floor(now / 60)}"
        day_key = f"ratelimit:msg:{user_id}:d:{local.date().isoformat()}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(minute_key)
                pipe.expire(minute_key, 120)
                pipe.incr(day_key)
                pipe.expire(day_key, 2 * 24 * 3600)
                minute_count, _, day_count, _ = await pipe.execute()
        except (RedisError, OSError):
            # Failing open: a broken limiter must not lock every employee out. With Redis down the
            # enqueue fails too, but the turn is stored and the requeuer enqueues it later.
            logger.warning("rate limiter unavailable; letting user_id=%s through", user_id, exc_info=True)
            return

        if day_count > self._per_day:
            RATE_LIMIT_REFUSALS.add(1, {"app.window": "day"})
            raise _limited("daily", self._per_day, next_midnight - now)
        if minute_count > self._per_minute:
            RATE_LIMIT_REFUSALS.add(1, {"app.window": "minute"})
            raise _limited("per-minute", self._per_minute, next_minute - now)


def _limited(window: str, limit: int, retry_after: float) -> ApiError:
    return ApiError(
        429,
        "RATE_LIMITED",
        f"{window} message limit of {limit} reached; try again later",
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )
