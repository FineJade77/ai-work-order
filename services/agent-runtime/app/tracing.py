"""Tracing for agent-runtime's own work: the queue, the run, the graph's nodes.

HTTP calls to the gateways and checkpoint queries are traced by the OTel agent (httpx, psycopg);
this adds what it cannot see. A task's span continues the trace app-service sent it from (the
message carries `traceparent`), so one employee message is one trace across the queue.
"""

import functools
import time
from collections.abc import Awaitable, Callable

from langgraph.errors import GraphInterrupt
from opentelemetry import propagate, trace
from opentelemetry.context import Context

from app.metrics import NODE_DURATION

tracer = trace.get_tracer("agent_runtime")


def messaging_attributes(stream: str, operation: str, **app: object) -> dict:
    """OTel messaging conventions (Redis Streams has no registered system name, so "redis"),
    plus this app's identifiers under app.*."""
    return {
        "messaging.system": "redis",
        "messaging.destination.name": stream,
        "messaging.operation.type": operation,
        **{f"app.{k}": str(v) for k, v in app.items() if v is not None},
    }


def context_of(fields: dict) -> Context:
    """The trace context a message was sent under (an empty context if it carries none)."""
    return propagate.extract(fields)


def inject_into(fields: dict) -> dict:
    """fields plus the current trace context, for a message this service sends."""
    propagate.inject(fields)
    return fields


def queue_wait_ms(message_id: str) -> int:
    """Time since the message was added: a Redis Stream id starts with its epoch milliseconds.
    For a redelivered message this includes the earlier attempts."""
    return max(0, int(time.time() * 1000) - int(message_id.split("-", 1)[0]))


def event(name: str, **attributes: object) -> None:
    """Something worth seeing on the current span's timeline (lock busy, stale copy, retry...)."""
    trace.get_current_span().add_event(name, {k: str(v) for k, v in attributes.items() if v is not None})


# State keys a node's result may set that say what it decided; recorded on the node's span.
_DECISION_KEYS = ("intent", "resolution", "outcome", "category", "knowledge_error", "ticket_id")


def traced_node(node: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Runs a graph node in a span named after it, and records its duration. functools.wraps
    keeps the signature, which LangGraph inspects to decide what to pass (state, runtime).

    An interrupt (await_feedback pausing the run for the employee's reply) is how the graph waits,
    not a failure: it is recorded as an event and the span is not marked as an error.
    """

    @functools.wraps(node)
    async def run(state, runtime):
        started = time.monotonic()
        failed = False
        with tracer.start_as_current_span(
            f"node {node.__name__}", attributes={"app.node": node.__name__}, record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = await node(state, runtime)
            except GraphInterrupt:
                span.add_event("interrupt", {"app.awaiting": "feedback"})
                raise
            except Exception as exc:
                failed = True
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                raise
            finally:
                NODE_DURATION.record(time.monotonic() - started, {"app.node": node.__name__, "app.failed": failed})
            for key in _DECISION_KEYS:
                if result.get(key) is not None:
                    span.set_attribute(f"app.{key}", str(result[key]))
            if "knowledge" in result and result["knowledge"] is not None:
                span.set_attribute("app.knowledge_hits", len(result["knowledge"]))
            return result

    return run
