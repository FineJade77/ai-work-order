import logging
from datetime import timedelta

import anyio
from opentelemetry.instrumentation.utils import suppress_instrumentation
from sqlalchemy import func, select

from app.clients.agent_bus import AgentBus, context_from
from app.core.config import Settings
from app.db.models import Conversation, Message, Tenant, Turn
from app.db.session import session_factory, tenant_scope
from app.repositories import conversations as repo

logger = logging.getLogger("app_service.requeuer")

# pg_try_advisory_xact_lock key: one replica sweeps at a time.
SWEEP_LOCK_KEY = 0x61707071
NO_RESULT_CODE = "NO_RESULT"
NO_RESULT_MESSAGE = "抱歉，这条消息处理失败了。请稍后重试；如果问题紧急，回复「提工单」转人工处理。"


class Requeuer:
    """Covers tasks Redis lost (e.g. in a failover): a turn still queued after requeue_after_seconds
    is enqueued again under the same task_id and seq, which agent-runtime treats idempotently (it resumes
    or just re-reports the result). After max_requeues the turn is marked failed."""

    def __init__(self, bus: AgentBus, settings: Settings):
        self._bus = bus
        self._settings = settings

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("requeue sweep failed")
            await anyio.sleep(self._settings.requeue_interval_seconds)

    async def sweep(self) -> None:
        # Untraced: the sweep's queries run every requeue_interval_seconds whether or not there is
        # work, and each would be a trace of its own. A requeue itself is traced, in the turn's
        # own trace (see AgentBus.enqueue's parent).
        with suppress_instrumentation():
            await self._sweep()

    async def _sweep(self) -> None:
        stale_before = func.now() - timedelta(seconds=self._settings.requeue_after_seconds)
        gave_up: list[Turn] = []
        Session = session_factory()
        async with Session() as session:
            if not await session.scalar(select(func.pg_try_advisory_xact_lock(SWEEP_LOCK_KEY))):
                return
            # turns has RLS, so the sweep visits tenants one by one; the tenant list itself is not scoped.
            for tenant_id in (await session.execute(select(Tenant.id))).scalars().all():
                async with tenant_scope(session, tenant_id):
                    stmt = (
                        select(Turn, Conversation.user_id, Message.content)
                        .join(Conversation, Conversation.id == Turn.conversation_id)
                        .join(Message, Message.id == Turn.user_message_id)
                        .where(Turn.status == "queued", Turn.updated_at < stale_before)
                        .with_for_update(of=Turn, skip_locked=True)
                    )
                    for turn, user_id, content in (await session.execute(stmt)).all():
                        if turn.enqueue_count > self._settings.max_requeues:
                            await repo.upsert_assistant_reply(session, turn, NO_RESULT_MESSAGE)
                            turn.status, turn.error_code = "failed", NO_RESULT_CODE
                            turn.completed_at = turn.updated_at = func.now()
                            gave_up.append(turn)
                            continue
                        # Enqueued before commit: if the commit then fails, the next sweep enqueues it
                        # again, and a duplicate task is harmless.
                        await self._bus.enqueue(
                            task_id=turn.id, seq=turn.seq, conversation_id=turn.conversation_id, tenant_id=tenant_id,
                            user_id=user_id, content=content, parent=context_from(turn.traceparent),
                            requeue=turn.enqueue_count,
                        )
                        turn.enqueue_count += 1
                        turn.updated_at = func.now()
                        logger.warning("requeued task_id=%s (enqueue #%d)", turn.id, turn.enqueue_count)
            await session.commit()
        for turn in gave_up:
            logger.error("gave up on task_id=%s after %d enqueues", turn.id, turn.enqueue_count)
            await self._bus.publish_error(
                conversation_id=turn.conversation_id, task_id=turn.id, code=NO_RESULT_CODE, message=NO_RESULT_MESSAGE
            )
