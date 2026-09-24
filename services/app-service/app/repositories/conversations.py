import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, Turn

# Callers open the session inside tenant_scope; RLS already confines every query to the tenant,
# and these functions add the per-user ownership check on top.


async def owned_conversation(session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation | None:
    return await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    )


async def list_owned(session: AsyncSession, user_id: uuid.UUID, limit: int) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    stmt = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
    return list((await session.execute(stmt)).scalars())


async def queued_turn(session: AsyncSession, conversation_id: uuid.UUID) -> Turn | None:
    return await session.scalar(select(Turn).where(Turn.conversation_id == conversation_id, Turn.status == "queued"))


async def next_turn_seq(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """Two concurrent sends can read the same value; uq_turns_conversation_seq lets only one insert it."""
    current = await session.scalar(select(func.max(Turn.seq)).where(Turn.conversation_id == conversation_id))
    return (current or 0) + 1


async def has_newer_turn(session: AsyncSession, turn: Turn) -> bool:
    return bool(
        await session.scalar(
            select(exists().where(Turn.conversation_id == turn.conversation_id, Turn.seq > turn.seq))
        )
    )


async def turn(session: AsyncSession, turn_id: uuid.UUID, *, for_update: bool = False) -> Turn | None:
    stmt = select(Turn).where(Turn.id == turn_id)
    return await session.scalar(stmt.with_for_update() if for_update else stmt)


async def upsert_assistant_reply(session: AsyncSession, turn: Turn, content: str) -> None:
    """One assistant message per turn. Overwrites, so a real answer replaces an earlier failure notice."""
    stmt = pg_insert(Message).values(
        id=uuid.uuid4(), conversation_id=turn.conversation_id, role="assistant", content=content, task_id=turn.id
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["task_id", "role"],
            index_where=Message.task_id.is_not(None),
            set_={"content": stmt.excluded.content},
        )
    )
