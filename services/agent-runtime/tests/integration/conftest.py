"""Needs Redis (docker compose up -d redis); the end-to-end tests also need Postgres with the
agent_runtime database migrated, and tool-gateway with its dependencies. Skipped when absent."""

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.worker.consumer import POLL_BLOCK_MS
from tests.integration.env import REDIS_URL, TOOL_GATEWAY_TOKEN, TOOL_GATEWAY_URL


@pytest.fixture
async def redis():
    client = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=POLL_BLOCK_MS / 1000 + 5)
    try:
        await client.ping()
    except (RedisConnectionError, OSError) as exc:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    await client.flushdb()  # the dedicated test database only
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def settings():
    return Settings(
        redis_url=REDIS_URL,
        checkpoint_database_url="unused",
        tool_gateway_url=TOOL_GATEWAY_URL,
        tool_gateway_service_token=TOOL_GATEWAY_TOKEN,
        ai_gateway_url="http://unused",
        ai_gateway_service_token="unused",
        worker_concurrency=4,
        task_timeout_seconds=3,
        conversation_lock_seconds=4,
        claim_idle_seconds=5,
        max_deliveries=3,
        consumer_name="test-consumer",
    )
