"""Execution events for one conversation, read by app-service and relayed to the browser over SSE.

Each conversation has its own Redis Stream rather than a Pub/Sub channel: Pub/Sub drops whatever
is published before a subscriber connects (e.g. between app-service's 202 and the browser opening
the SSE connection), while a stream can be read from any position and resumed via Last-Event-ID.

Event types (the `type` field; `data` is a JSON object):
  retrieving        searching the knowledge base
  answer_chunk      {"delta"}                 incremental answer text
  answer            {"content"}               the complete answer
  reply             {"content"}               a fixed assistant message (greeting, resolved ack, search down,
                                              token budget used up)
  ticket_created    {"ticket_id", "content"}
  resumed           a crashed run of this task restarted; discard its partial answer_chunks
  error             {"code", "message"}       the task was given up on
  done              {"outcome"}               awaiting_feedback | resolved | replied | ticket_created
                                              | knowledge_unavailable | budget_exceeded

Every task also ends with one entry on the global agent_results stream, written in the same
transaction as its done/error event. app-service consumes it with a consumer group to store the
assistant's reply and close the turn, which must not depend on a browser still being connected.
"""

import json
import uuid

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

from app.schemas.tasks import AgentTask
from app.tracing import inject_into

EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 24 * 3600
RESULT_STREAM = "agent_results"
# app-service consumes the results with this group; only watched from here (queue metrics).
RESULT_GROUP = "app_service"
# Far above any realistic consumer backlog; trimming only bounds memory if app-service is down for long.
RESULT_STREAM_MAXLEN = 100_000


def event_stream_key(conversation_id: uuid.UUID | str) -> str:
    return f"conversation:{conversation_id}:events"


class EventPublisher:
    def __init__(self, redis: Redis, task: AgentTask):
        self._redis = redis
        self._task = task
        self._key = event_stream_key(task.conversation_id)

    async def publish(self, type_: str, **data: object) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            self._event(pipe, type_, data)
            await pipe.execute()

    async def finish(self, *, outcome: str, content: str, ticket_id: str | None) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            self._event(pipe, "done", {"outcome": outcome})
            self._result(pipe, status="done", outcome=outcome, content=content, ticket_id=ticket_id or "")
            await pipe.execute()

    async def fail(self, *, code: str, message: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            self._event(pipe, "error", {"code": code, "message": message})
            self._result(pipe, status="failed", error_code=code, content=message)
            await pipe.execute()

    def _event(self, pipe: Pipeline, type_: str, data: dict) -> None:
        pipe.xadd(
            self._key,
            {"task_id": str(self._task.task_id), "type": type_, "data": json.dumps(data, ensure_ascii=False)},
            maxlen=EVENT_STREAM_MAXLEN,
            approximate=True,
        )
        pipe.expire(self._key, EVENT_STREAM_TTL_SECONDS)

    def _result(self, pipe: Pipeline, **fields: str) -> None:
        task = self._task
        identity = {"task_id": task.task_id, "conversation_id": task.conversation_id, "tenant_id": task.tenant_id}
        # With the run's trace context, so app-service's handling of it links back to this run.
        message = inject_into({**{k: str(v) for k, v in identity.items()}, **fields})
        pipe.xadd(RESULT_STREAM, message, maxlen=RESULT_STREAM_MAXLEN, approximate=True)
