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

# The queue's name in spans. The tasks are in one stream per tenant (task_stream), which
# agent-runtime serves in turn so that one tenant's backlog does not delay the others.
TASK_STREAM = "agent_tasks"
ACTIVE_TENANTS = "agent_sched:active"  # ZSET: tenants with undelivered tasks -> their virtual time
KNOWN_TENANTS = "agent_sched:tenants"  # ZSET: every tenant seen -> virtual time it has been served up to
VIRTUAL_TIME = "agent_sched:vtime"
WAKE_STREAM = "agent_sched:wake"
RESULT_STREAM = "agent_results"
RESULT_GROUP = "app_service"
EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 24 * 3600
TERMINAL_EVENTS = frozenset({"done", "error"})
FLOOR_TTL_SECONDS = 24 * 3600

# One script, so a worker that sees the task also sees a floor at least as high as its seq (any
# copy of an older turn still queued is then skipped rather than run after it), and a tenant with
# a task waiting is always among the active ones.
# - The floor is raised, never lowered, and its TTL refreshed either way.
# - A tenant with nothing queued joins the active tenants at the current virtual time, or where it
#   was last served up to if that is later: time spent idle is not credit to jump the queue with.
# - The wake-up is for workers idle on WAKE_STREAM.
# KEYS: floor, the tenant's stream, active, known, vtime, wake
# ARGV: seq, floor TTL, tenant, then the message's fields and values
_ENQUEUE = """
local floor = tonumber(redis.call('GET', KEYS[1]) or '0')
if tonumber(ARGV[1]) > floor then redis.call('SET', KEYS[1], ARGV[1]) end
redis.call('EXPIRE', KEYS[1], ARGV[2])
local id = redis.call('XADD', KEYS[2], '*', unpack(ARGV, 4))
if not redis.call('ZSCORE', KEYS[3], ARGV[3]) then
  local vtime = tonumber(redis.call('GET', KEYS[5]) or '0')
  local served = tonumber(redis.call('ZSCORE', KEYS[4], ARGV[3]) or '0')
  local start = string.format('%.17g', math.max(vtime, served))
  redis.call('ZADD', KEYS[3], start, ARGV[3])
  redis.call('ZADD', KEYS[4], start, ARGV[3])
end
redis.call('XADD', KEYS[6], 'MAXLEN', '~', 100, '*', 'tenant', ARGV[3])
return id
"""


def task_stream(tenant_id: uuid.UUID | str) -> str:
    return f"agent_tasks:t:{tenant_id}"


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
        approval: dict[str, str] | None = None,
    ) -> None:
        """parent: the trace to send under; the current one by default. The requeuer passes the
        turn's original trace, so a requeued task still belongs to it.

        approval: for an approval turn, {approval_id, decision, note}; the task (kind "approval")
        resumes the agent run waiting for that approval, and content is empty."""
        fields = {
            "task_id": task_id,
            "seq": seq,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "content": content,
        }
        if approval is not None:
            fields |= {"kind": "approval", **approval}
        attributes = messaging_attributes(
            TASK_STREAM, "send", task_id=task_id, seq=seq, conversation_id=conversation_id, requeue=requeue
        )
        with _tracer.start_as_current_span(
            f"send {TASK_STREAM}", context=parent, kind=SpanKind.PRODUCER, attributes=attributes
        ) as span:
            message = {k: str(v) for k, v in fields.items()}
            propagate.inject(message)  # the consumer's span becomes a child of this one
            message_id = await self.redis.eval(
                _ENQUEUE, 6,
                floor_key(conversation_id), task_stream(tenant_id), ACTIVE_TENANTS, KNOWN_TENANTS, VIRTUAL_TIME, WAKE_STREAM,
                seq, FLOOR_TTL_SECONDS, str(tenant_id), *(item for pair in message.items() for item in pair),
            )
            span.set_attribute("messaging.message.id", message_id)

    async def publish_error(self, *, conversation_id: uuid.UUID, task_id: uuid.UUID, code: str, message: str) -> None:
        """For a turn app-service gives up on, so an open SSE connection ends instead of waiting."""
        key = event_stream_key(conversation_id)
        data = json.dumps({"code": code, "message": message}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"task_id": str(task_id), "type": "error", "data": data}, maxlen=EVENT_STREAM_MAXLEN, approximate=True)
            pipe.expire(key, EVENT_STREAM_TTL_SECONDS)
            await pipe.execute()
