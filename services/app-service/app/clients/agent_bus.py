"""app-service's side of the Redis contract with agent-runtime (services/agent-runtime/app/events.py
and app/worker/queue.py). Services share no code, so the names and fields are repeated here."""

import json
import uuid

from redis.asyncio import Redis

TASK_STREAM = "agent_tasks"
RESULT_STREAM = "agent_results"
RESULT_GROUP = "app_service"
EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 24 * 3600
TERMINAL_EVENTS = frozenset({"done", "error"})


def event_stream_key(conversation_id: uuid.UUID | str) -> str:
    return f"conversation:{conversation_id}:events"


class AgentBus:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def enqueue(
        self, *, task_id: uuid.UUID, conversation_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID, content: str
    ) -> None:
        fields = {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "content": content,
        }
        await self.redis.xadd(TASK_STREAM, {k: str(v) for k, v in fields.items()})

    async def publish_error(self, *, conversation_id: uuid.UUID, task_id: uuid.UUID, code: str, message: str) -> None:
        """For a turn app-service gives up on, so an open SSE connection ends instead of waiting."""
        key = event_stream_key(conversation_id)
        data = json.dumps({"code": code, "message": message}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"task_id": str(task_id), "type": "error", "data": data}, maxlen=EVENT_STREAM_MAXLEN, approximate=True)
            pipe.expire(key, EVENT_STREAM_TTL_SECONDS)
            await pipe.execute()
