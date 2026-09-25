"""app-service's side of the Redis contract with agent-runtime (services/agent-runtime/app/events.py
and app/worker/queue.py). Services share no code, so the names and fields are repeated here.

Messages carry the W3C trace context (`traceparent`, and `tracestate` when there is one) next to
their own fields, so work on either side of the queue joins the same trace.
"""

import json
import uuid

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, SpanKind
from redis.asyncio import Redis

TASK_STREAM = "agent_tasks"
RESULT_STREAM = "agent_results"
RESULT_GROUP = "app_service"
EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 24 * 3600
TERMINAL_EVENTS = frozenset({"done", "error"})
FLOOR_TTL_SECONDS = 24 * 3600

# Raises the floor, never lowers it, and refreshes its TTL either way.
_RAISE_FLOOR = """
local current = tonumber(redis.call('get', KEYS[1]) or '0')
if tonumber(ARGV[1]) > current then redis.call('set', KEYS[1], ARGV[1]) end
redis.call('expire', KEYS[1], ARGV[2])
"""


def event_stream_key(conversation_id: uuid.UUID | str) -> str:
    return f"conversation:{conversation_id}:events"


def floor_key(conversation_id: uuid.UUID | str) -> str:
    """The seq of the conversation's newest enqueued turn; agent-runtime skips tasks below it."""
    return f"agent:floor:{conversation_id}"


_tracer = trace.get_tracer("app_service.agent_bus")


def current_traceparent() -> str | None:
    """The current trace context as a W3C traceparent, or None when not tracing."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def context_from(traceparent: str | None) -> Context | None:
    return propagate.extract({"traceparent": traceparent}) if traceparent else None


def link_to(fields: dict) -> list[Link]:
    """A link to the trace a message was sent from, if it carries one."""
    span_context = trace.get_current_span(propagate.extract(fields)).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []


def messaging_attributes(stream: str, operation: str, **app: object) -> dict:
    """OTel messaging conventions (Redis Streams has no registered system name, so "redis"),
    plus this app's identifiers under app.*."""
    return {
        "messaging.system": "redis",
        "messaging.destination.name": stream,
        "messaging.operation.type": operation,
        **{f"app.{k}": str(v) for k, v in app.items() if v is not None},
    }


class AgentBus:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def enqueue(
        self,
        *,
        task_id: uuid.UUID,
        seq: int,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        content: str,
        parent: Context | None = None,
        requeue: int | None = None,
    ) -> None:
        """parent: the trace to send under; the current one by default. The requeuer passes the
        turn's original trace, so a requeued task still belongs to it."""
        fields = {
            "task_id": task_id,
            "seq": seq,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "content": content,
        }
        attributes = messaging_attributes(
            TASK_STREAM, "send", task_id=task_id, seq=seq, conversation_id=conversation_id, requeue=requeue
        )
        with _tracer.start_as_current_span(
            f"send {TASK_STREAM}", context=parent, kind=SpanKind.PRODUCER, attributes=attributes
        ) as span:
            message = {k: str(v) for k, v in fields.items()}
            propagate.inject(message)  # the consumer's span becomes a child of this one
            # One transaction: a worker that sees this task also sees a floor at least this high, so
            # any copy of an older turn still in the stream is skipped rather than run after it.
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.eval(_RAISE_FLOOR, 1, floor_key(conversation_id), seq, FLOOR_TTL_SECONDS)
                pipe.xadd(TASK_STREAM, message)
                _, message_id = await pipe.execute()
            span.set_attribute("messaging.message.id", message_id)

    async def publish_error(self, *, conversation_id: uuid.UUID, task_id: uuid.UUID, code: str, message: str) -> None:
        """For a turn app-service gives up on, so an open SSE connection ends instead of waiting."""
        key = event_stream_key(conversation_id)
        data = json.dumps({"code": code, "message": message}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"task_id": str(task_id), "type": "error", "data": data}, maxlen=EVENT_STREAM_MAXLEN, approximate=True)
            pipe.expire(key, EVENT_STREAM_TTL_SECONDS)
            await pipe.execute()
