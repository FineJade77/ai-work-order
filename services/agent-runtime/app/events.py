"""Execution events for one conversation, read by app-service and relayed to the browser over SSE.

Each conversation has its own Redis Stream rather than a Pub/Sub channel: Pub/Sub drops whatever
is published before a subscriber connects (e.g. between app-service's 202 and the browser opening
the SSE connection), while a stream can be read from any position and resumed via Last-Event-ID.

Event types (the `type` field; `data` is a JSON object):
  retrieving        searching the knowledge base
  answer_chunk      {"delta"}                 incremental answer text
  answer            {"content"}               the complete answer
  reply             {"content"}               a fixed assistant message (greeting, resolved ack)
  ticket_created    {"ticket_id", "content"}
  resumed           a crashed run of this task restarted; discard its partial answer_chunks
  error             {"code", "message"}       the task was given up on
  done              {"outcome"}               awaiting_feedback | resolved | replied | ticket_created
"""

import json
import uuid

from redis.asyncio import Redis

EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 24 * 3600


def event_stream_key(conversation_id: uuid.UUID | str) -> str:
    return f"conversation:{conversation_id}:events"


class EventPublisher:
    def __init__(self, redis: Redis, conversation_id: uuid.UUID, task_id: uuid.UUID):
        self._redis = redis
        self._key = event_stream_key(conversation_id)
        self._task_id = str(task_id)

    async def publish(self, type_: str, **data: object) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self._key,
                {"task_id": self._task_id, "type": type_, "data": json.dumps(data, ensure_ascii=False)},
                maxlen=EVENT_STREAM_MAXLEN,
                approximate=True,
            )
            pipe.expire(self._key, EVENT_STREAM_TTL_SECONDS)
            await pipe.execute()
