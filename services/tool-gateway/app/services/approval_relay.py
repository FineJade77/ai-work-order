"""The approval outbox relay: publishes decided approvals to the approval_events stream and expires
approvals nobody decided in time.

Transactional outbox: a decision and the outbox row announcing it are committed together
(repositories/approvals.py), and this relay turns rows into events. A crash between publishing
and marking the row done publishes again, so delivery is at least once and consumers must be
idempotent (app-service deduplicates on approval_id). An approval's expiry is an outbox row too,
due at its expires_at, so no cross-tenant scan of approvals is needed.

Rows are taken one at a time with FOR UPDATE SKIP LOCKED, so any number of replicas can run this.
"""

import logging

import anyio
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import Link, SpanKind
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.sql import func

from app.db.models import ApprovalOutbox
from app.db.session import session_factory
from app.repositories import approvals

logger = logging.getLogger("tool_gateway.approval_relay")
tracer = trace.get_tracer("tool_gateway.approvals")

EVENT_STREAM = "approval_events"
EVENT_STREAM_MAXLEN = 100_000
BATCH = 50


class ApprovalRelay:
    def __init__(self, redis: Redis, interval_s: float):
        self._redis = redis
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except (RedisError, OSError):
                logger.warning("approval relay: redis unavailable; will retry", exc_info=True)
            except Exception:
                logger.exception("approval relay round failed; will retry")
            await anyio.sleep(self._interval_s)

    async def run_once(self) -> int:
        """Handles due outbox rows until none are left (or BATCH). Returns how many."""
        handled = 0
        while handled < BATCH and await self._take_one():
            handled += 1
        return handled

    async def _take_one(self) -> bool:
        Session = session_factory()
        # The poll runs every few seconds with or without work; only handling a row is traced.
        with suppress_instrumentation():
            async with Session() as session:
                row = await session.scalar(
                    select(ApprovalOutbox)
                    .where(ApprovalOutbox.done_at.is_(None), ApprovalOutbox.due_at <= func.now())
                    .order_by(ApprovalOutbox.due_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return False
                await self._handle(str(row.tenant_id), str(row.approval_id), row.kind)
                row.done_at = func.now()
                await session.commit()  # after the publish: a crash in between publishes again
        return True

    async def _handle(self, tenant_id: str, approval_id: str, kind: str) -> None:
        if kind == "expire":
            # A no-op if it was decided meanwhile; otherwise the expiry is published next round.
            with tracer.start_as_current_span("expire approval", context=Context(), attributes={
                "app.approval_id": approval_id, "app.tenant_id": tenant_id,
            }) as span:
                expired = await approvals.decide(tenant_id, approval_id, status="expired", decided_by=None, note=None)
                span.set_attribute("app.expired", expired)
            return
        approval = await approvals.get(tenant_id, approval_id)
        links = _link_to(approval.traceparent)
        with tracer.start_as_current_span(
            f"send {EVENT_STREAM}", context=Context(), kind=SpanKind.PRODUCER, links=links,
            attributes={
                "messaging.system": "redis", "messaging.destination.name": EVENT_STREAM,
                "messaging.operation.type": "send", "app.approval_id": approval_id, "app.status": approval.status,
                "app.tenant_id": tenant_id,
            },
        ) as span:
            fields = {
                "approval_id": approval.id,
                "tenant_id": tenant_id,
                "conversation_id": approval.session_id,
                "requester_id": approval.requester_id,
                "tool_name": approval.tool_name,
                "status": approval.status,
                "decided_by": approval.decided_by or "",
                "note": approval.note or "",
                # The trace the approval was requested in, so the resumed run can link back to it.
                "approval_traceparent": approval.traceparent or "",
            }
            propagate.inject(fields)  # this span, as the parent of the consumer's
            message_id = await self._redis.xadd(EVENT_STREAM, fields, maxlen=EVENT_STREAM_MAXLEN, approximate=True)
            span.set_attribute("messaging.message.id", message_id)


def _link_to(traceparent: str | None) -> list[Link]:
    if not traceparent:
        return []
    span_context = trace.get_current_span(propagate.extract({"traceparent": traceparent})).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []
