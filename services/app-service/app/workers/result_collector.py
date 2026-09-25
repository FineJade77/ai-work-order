import logging
import time
import uuid

import anyio
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import func

from app.clients.agent_bus import RESULT_GROUP, RESULT_STREAM, link_to, messaging_attributes
from app.db.session import session_factory, tenant_scope
from app.repositories import conversations as repo

logger = logging.getLogger("app_service.results")
_tracer = trace.get_tracer("app_service.results")

READ_BLOCK_MS = 5000
# A result a crashed replica took but never acknowledged is taken over after this long.
CLAIM_IDLE_MS = 60_000
CLAIM_INTERVAL_S = 30


class ResultCollector:
    """Stores each turn's outcome from agent_results: the assistant reply and the turn's status.

    Consumer group, so replicas share the work and an unacknowledged result is redelivered.
    Handling is idempotent (the reply is upserted per turn), since delivery is at least once.
    A late answer replaces a failed turn's notice only while that turn is still the newest.
    """

    def __init__(self, redis: Redis, consumer: str):
        self._redis = redis
        self._consumer = consumer
        self._next_claim_at = 0.0

    async def run(self) -> None:
        try:
            await self._redis.xgroup_create(RESULT_STREAM, RESULT_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        while True:
            try:
                for entry_id, fields in await self._receive():
                    await self._process(entry_id, fields)
            except (RedisError, OSError):
                logger.exception("reading agent_results failed; retrying")
                await anyio.sleep(1)

    async def _receive(self) -> list:
        entries = []
        if time.monotonic() >= self._next_claim_at:
            self._next_claim_at = time.monotonic() + CLAIM_INTERVAL_S
            _, entries, *_ = await self._redis.xautoclaim(
                RESULT_STREAM, RESULT_GROUP, self._consumer, CLAIM_IDLE_MS, start_id="0-0", count=50
            )
        if entries:
            return entries
        response = await self._redis.xreadgroup(
            RESULT_GROUP, self._consumer, {RESULT_STREAM: ">"}, count=50, block=READ_BLOCK_MS
        )
        if not response:
            return []
        return next(iter(response.values())) if isinstance(response, dict) else response[0][1]

    async def _process(self, entry_id: str, fields: dict) -> None:
        # A trace of its own, linked to the agent run that sent the result: storing it is separate
        # work, possibly by another replica, possibly much later (redelivery).
        attributes = messaging_attributes(
            RESULT_STREAM, "process", task_id=fields.get("task_id"), status=fields.get("status")
        )
        attributes["messaging.message.id"] = entry_id
        with _tracer.start_as_current_span(
            f"process {RESULT_STREAM}", kind=SpanKind.CONSUMER, links=link_to(fields), attributes=attributes
        ):
            await self._process_traced(entry_id, fields)

    async def _process_traced(self, entry_id: str, fields: dict) -> None:
        try:
            await self.handle(fields)
        except (KeyError, ValueError) as exc:
            logger.error("malformed agent_results entry %s: %r; dropping it", entry_id, fields)
            _mark_failed(exc)
        except Exception as exc:
            logger.exception("storing result %s failed; it stays pending for redelivery", entry_id)
            _mark_failed(exc)
            return
        await self._redis.xack(RESULT_STREAM, RESULT_GROUP, entry_id)

    async def handle(self, fields: dict) -> None:
        task_id, tenant_id = uuid.UUID(fields["task_id"]), uuid.UUID(fields["tenant_id"])
        status = fields["status"]
        if status not in ("done", "failed"):
            raise ValueError(f"unknown status {status!r}")
        Session = session_factory()
        async with Session() as session, tenant_scope(session, tenant_id):
            turn = await repo.turn(session, task_id, for_update=True)
            if turn is None:
                logger.warning("result for unknown turn task_id=%s", task_id)
                return
            if turn.status == "done":  # a redelivered result; a later failure never undoes an answer
                return
            if turn.status == "failed" and await repo.has_newer_turn(session, turn):
                # The employee has moved on since this turn failed; an answer now would land out of order.
                logger.warning("dropping late %s result for superseded task_id=%s", status, task_id)
                return
            await repo.upsert_assistant_reply(session, turn, fields["content"])
            turn.status = status
            if status == "done":
                turn.outcome = fields["outcome"]
                turn.ticket_id = uuid.UUID(fields["ticket_id"]) if fields.get("ticket_id") else None
                turn.error_code = None
            else:
                turn.error_code = fields.get("error_code") or "TASK_FAILED"
            turn.completed_at = turn.updated_at = func.now()
            await session.commit()


def _mark_failed(exc: Exception) -> None:
    """Handled here rather than raised, so the span would otherwise look like a success."""
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(StatusCode.ERROR, str(exc))
