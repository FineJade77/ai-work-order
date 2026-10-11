import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from redis.asyncio import Redis

from app.clients.agent_bus import task_stream
from app.core.errors import ApiError
from app.services.rate_limit import MessageRateLimiter
from tests.conftest import metric_total
from tests.integration.test_conversations import history, send, start_conversation

pytestmark = pytest.mark.anyio

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 10:00:30 in Shanghai: 29.5s to the next minute, 14h minus 30s to the next local midnight.
NOW = datetime(2026, 9, 25, 10, 0, 30, 500000, tzinfo=SHANGHAI).timestamp()


def limiter(redis, per_minute=2, per_day=100):
    return MessageRateLimiter(redis, per_minute=per_minute, per_day=per_day, timezone="Asia/Shanghai")


async def refused(check) -> ApiError:
    with pytest.raises(ApiError) as exc:
        await check
    assert (exc.value.http_status, exc.value.code) == (429, "RATE_LIMITED")
    return exc.value


async def test_per_minute_limit_resets_with_the_next_minute(redis):
    user, limit = uuid.uuid4(), limiter(redis)
    for _ in range(2):
        await limit.check(user, now=NOW)
    exc = await refused(limit.check(user, now=NOW + 10))
    assert exc.headers == {"Retry-After": "20"}  # 19.5s left in the minute, rounded up
    await limit.check(user, now=NOW + 30)  # next minute
    await limit.check(uuid.uuid4(), now=NOW)  # other users have their own count


async def test_daily_limit_holds_until_local_midnight(redis):
    user, limit = uuid.uuid4(), limiter(redis, per_minute=100, per_day=3)
    for minute in range(3):
        await limit.check(user, now=NOW + 60 * minute)
    exc = await refused(limit.check(user, now=NOW + 600))
    assert exc.headers == {"Retry-After": str(14 * 3600 - 630)}
    tomorrow = datetime(2026, 9, 26, 0, 0, 1, tzinfo=SHANGHAI).timestamp()
    await limit.check(user, now=tomorrow)


async def test_an_unreachable_redis_lets_messages_through():
    broken = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    try:
        await limiter(broken, per_minute=1).check(uuid.uuid4())
    finally:
        await broken.aclose()


async def test_a_refused_message_is_not_stored_or_enqueued(app, client, redis, tenant, login):
    app.state.rate_limiter = limiter(redis, per_minute=1)
    alice = await login(tenant.alice)
    conversation = await start_conversation(client, alice)
    assert (await send(client, alice, conversation)).status_code == 202
    before = await history(client, alice, conversation)

    resp = await send(client, alice, await start_conversation(client, alice), "新会话也算在同一个人头上")
    assert (resp.status_code, resp.json()["error"]["code"]) == (429, "RATE_LIMITED")
    assert int(resp.headers["Retry-After"]) >= 1
    assert await history(client, alice, conversation) == before
    assert len(await redis.xrange(task_stream(tenant.id))) == 1


async def test_refusals_are_counted_by_window(redis):
    before = metric_total("app.rate_limit.refusals", app__window="minute")
    user, limit = uuid.uuid4(), limiter(redis, per_minute=1)
    await limit.check(user, now=NOW)
    await refused(limit.check(user, now=NOW))
    assert metric_total("app.rate_limit.refusals", app__window="minute") - before == 1
