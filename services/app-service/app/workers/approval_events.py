"""Turns approval decisions into agent work.

tool-gateway publishes each decision (approved, rejected, expired) on the approval_events stream
(its outbox relay: at least once). For each one this opens an approval turn in the conversation
that asked (next seq, so it is ordered with the employee's messages) and enqueues its task, which
resumes the agent run paused at await_approval.

Idempotent: turns.approval_id is unique, so a redelivered event finds its turn and stops. If the
conversation has a turn in flight (one queued turn per conversation), the event is left pending and
retried once it has been idle for CLAIM_IDLE_MS; the decision is not lost, only delayed.
"""

import logging
import time
import uuid

import anyio
from opentelemetry import propagate, trace
from opentelemetry.trace import Link, SpanKind
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clients.agent_bus import AgentBus, current_traceparent, messaging_attributes
from app.db.models import Conversation, Turn
from app.db.session import session_factory, tenant_scope
from app.repositories import conversations as repo

logger = logging.getLogger("app_service.approval_events")
_tracer = trace.get_tracer("app_service.approvals")

APPROVAL_STREAM = "approval_events"
GROUP = "app_service"
DECISIONS = frozenset({"approved", "rejected", "expired"})
READ_BLOCK_MS = 5000
CLAIM_IDLE_MS = 30_000
CLAIM_INTERVAL_S = 10


class TurnInFlight(Exception):
    """The conversation already has a queued turn; try again later."""


def approval_fields(turn: Turn) -> dict[str, str] | None:
    """What an approval turn's task carries (see AgentBus.enqueue), or None for a message turn."""
    if turn.kind != "approval":
        return None
    return {"approval_id": str(turn.approval_id), "decision": turn.approval_decision, "note": turn.approval_note or ""}


class ApprovalEventConsumer:
    def __init__(self, redis: Redis, bus: AgentBus, consumer: str):
        self._redis = redis
        self._bus = bus
        self._consumer = consumer
        self._next_claim_at = 0.0

    async def run(self) -> None:
        try:
            await self._redis.xgroup_create(APPROVAL_STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        while True:
            try:
                for entry_id, fields in await self._receive():
                    await self._process(entry_id, fields)
            except (RedisError, OSError):
                logger.exception("reading approval_events failed; retrying")
                await anyio.sleep(1)

    async def _receive(self) -> list:
        if time.monotonic() >= self._next_claim_at:
            self._next_claim_at = time.monotonic() + CLAIM_INTERVAL_S
            _, entries, *_ = await self._redis.xautoclaim(
                APPROVAL_STREAM, GROUP, self._consumer, CLAIM_IDLE_MS, start_id="0-0", count=50
            )
            if entries:
                return entries
        response = await self._redis.xreadgroup(
            GROUP, self._consumer, {APPROVAL_STREAM: ">"}, count=50, block=READ_BLOCK_MS
        )
        if not response:
            return []
        return next(iter(response.values())) if isinstance(response, dict) else response[0][1]

    async def _process(self, entry_id: str, fields: dict) -> None:
        # A child of the relay's publish span, and linked to the trace the approval was requested
        # in, so the resumed run is one hop from the original conversation turn.
        attributes = messaging_attributes(
            APPROVAL_STREAM, "process", approval_id=fields.get("approval_id"), status=fields.get("status"),
            conversation_id=fields.get("conversation_id"),
        )
        attributes["messaging.message.id"] = entry_id
        with _tracer.start_as_current_span(
            f"process {APPROVAL_STREAM}", context=propagate.extract(fields), kind=SpanKind.CONSUMER,
            links=_links(fields.get("approval_traceparent")), attributes=attributes,
        ) as span:
            try:
                opened = await self.handle(fields)
            except TurnInFlight:
                span.add_event("turn_in_flight")
                logger.info("approval %s waits for the conversation's turn in flight", fields.get("approval_id"))
                return  # left pending: reclaimed after CLAIM_IDLE_MS
            except (KeyError, ValueError):
                logger.error("malformed approval_events entry %s: %r; dropping it", entry_id, fields)
            except Exception:
                logger.exception("handling approval event %s failed; it stays pending", entry_id)
                return
            else:
                span.set_attribute("app.turn_opened", opened)
            await self._redis.xack(APPROVAL_STREAM, GROUP, entry_id)

    async def handle(self, fields: dict) -> bool:
        """Opens the approval turn and enqueues its task. False if the event was seen before or
        its conversation is gone."""
        tenant_id, conversation_id = uuid.UUID(fields["tenant_id"]), uuid.UUID(fields["conversation_id"])
        approval_id, decision = uuid.UUID(fields["approval_id"]), fields["status"]
        if decision not in DECISIONS:
            raise ValueError(f"not a decision: {decision!r}")
        turn = Turn(
            id=uuid.uuid4(), tenant_id=tenant_id, conversation_id=conversation_id, kind="approval",
            approval_id=approval_id, approval_decision=decision, approval_note=fields.get("note") or None,
            traceparent=current_traceparent(),
        )
        Session = session_factory()
        async with Session() as session, tenant_scope(session, tenant_id):
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                logger.warning("approval %s for unknown conversation %s", approval_id, conversation_id)
                return False
            if await session.scalar(select(Turn.id).where(Turn.approval_id == approval_id)):
                return False  # a redelivered event: its turn exists (and was enqueued, or will be requeued)
            turn.seq = await repo.next_turn_seq(session, conversation_id)
            session.add(turn)
            try:
                await session.commit()
            except IntegrityError as exc:
                if "uq_turns_approval" in str(exc):
                    return False  # a concurrent delivery won
                if any(c in str(exc) for c in ("uq_turns_one_queued_per_conversation", "uq_turns_conversation_seq")):
                    raise TurnInFlight() from exc
                raise
            user_id = conversation.user_id
        await self._bus.enqueue(
            task_id=turn.id, seq=turn.seq, conversation_id=conversation_id, tenant_id=tenant_id, user_id=user_id,
            content="", approval=approval_fields(turn),
        )
        return True


def _links(traceparent: str | None) -> list[Link]:
    if not traceparent:
        return []
    span_context = trace.get_current_span(propagate.extract({"traceparent": traceparent})).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []
