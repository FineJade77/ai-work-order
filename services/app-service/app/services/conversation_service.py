import logging
import uuid

from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from app.clients.agent_bus import AgentBus, current_traceparent
from app.core.errors import ApiError
from app.core.security import CurrentUser
from app.db.models import Conversation, Message, Turn
from app.db.session import session_factory, tenant_scope
from app.repositories import conversations as repo

logger = logging.getLogger("app_service.conversations")

_NOT_FOUND = ApiError(404, "CONVERSATION_NOT_FOUND", "conversation not found")


async def create(user: CurrentUser) -> Conversation:
    conversation = Conversation(id=uuid.uuid4(), tenant_id=user.tenant_id, user_id=user.id)
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        session.add(conversation)
        await session.flush()
        await session.refresh(conversation)  # server defaults; before commit, while the tenant scope still applies
        await session.commit()
    return conversation


async def list_mine(user: CurrentUser, limit: int) -> list[Conversation]:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        return await repo.list_owned(session, user.id, limit)


async def history(user: CurrentUser, conversation_id: uuid.UUID) -> tuple[list[Message], Turn | None]:
    """Messages plus the turn still in flight, if any, so a reloaded page can reattach its SSE stream."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        if await repo.owned_conversation(session, conversation_id, user.id) is None:
            raise _NOT_FOUND
        return await repo.messages(session, conversation_id), await repo.queued_turn(session, conversation_id)


async def send_message(user: CurrentUser, conversation_id: uuid.UUID, content: str, bus: AgentBus) -> tuple[uuid.UUID, uuid.UUID]:
    """Stores the message and its turn, then hands the turn to agent-runtime. Returns (task_id, message_id)."""
    turn_id, message_id = uuid.uuid4(), uuid.uuid4()
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        if await repo.owned_conversation(session, conversation_id, user.id) is None:
            raise _NOT_FOUND
        seq = await repo.next_turn_seq(session, conversation_id)
        session.add(Message(id=message_id, conversation_id=conversation_id, role="user", content=content, task_id=turn_id))
        await session.flush()
        session.add(
            Turn(
                id=turn_id, tenant_id=user.tenant_id, conversation_id=conversation_id, user_message_id=message_id,
                seq=seq, traceparent=current_traceparent(),
            )
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            # Either constraint means another message for this conversation got in first.
            if any(c in str(exc) for c in ("uq_turns_one_queued_per_conversation", "uq_turns_conversation_seq")):
                raise ApiError(409, "TURN_IN_PROGRESS", "the previous message is still being answered") from exc
            raise
    try:
        await bus.enqueue(
            task_id=turn_id, seq=seq, conversation_id=conversation_id, tenant_id=user.tenant_id, user_id=user.id,
            content=content,
        )
    except (RedisError, OSError):
        # The turn is committed; the requeuer enqueues it once it has waited requeue_after_seconds.
        logger.exception("enqueue failed task_id=%s; left to the requeuer", turn_id)
    return turn_id, message_id


async def turn_for_stream(user: CurrentUser, conversation_id: uuid.UUID, task_id: uuid.UUID) -> Turn:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, user.tenant_id):
        if await repo.owned_conversation(session, conversation_id, user.id) is None:
            raise _NOT_FOUND
        turn = await repo.turn(session, task_id)
    if turn is None or turn.conversation_id != conversation_id:
        raise ApiError(404, "TURN_NOT_FOUND", "turn not found")
    return turn
