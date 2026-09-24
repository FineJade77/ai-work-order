import uuid
from datetime import datetime

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Ticket, TicketEvent
from app.db.session import session_factory, tenant_scope

_IDEMPOTENCY_INDEX = {
    "index_elements": ["tenant_id", "idempotency_key"],
    "index_where": Ticket.idempotency_key.is_not(None),
}


async def create_idempotent(
    *, tenant_id: str, requester_id: str, conversation_id: str | None, category: str, summary: str, idempotency_key: str
) -> tuple[Ticket, bool]:
    """Returns (ticket, created). A retry with the same key returns the original ticket."""
    values = dict(
        tenant_id=tenant_id,
        requester_id=requester_id,
        conversation_id=conversation_id,
        category=category,
        summary=summary,
        idempotency_key=idempotency_key,
    )
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        insert = pg_insert(Ticket).values(**values).on_conflict_do_nothing(**_IDEMPOTENCY_INDEX)
        new_id = (await session.execute(insert.returning(Ticket.id))).scalar_one_or_none()
        created = new_id is not None
        if created:
            session.add(
                TicketEvent(
                    tenant_id=tenant_id, ticket_id=new_id, action="create",
                    from_status=None, to_status="open", actor_id=requester_id,
                )
            )
        # Same transaction as the insert: tenant_scope is transaction-local.
        ticket = (
            await session.execute(select(Ticket).where(Ticket.idempotency_key == idempotency_key))
        ).scalar_one()
        await session.commit()
    return ticket, created


async def get(tenant_id: str, ticket_id: str) -> Ticket | None:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        return await session.get(Ticket, ticket_id)


async def events(tenant_id: str, ticket_id: str) -> list[TicketEvent]:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        stmt = (
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at, TicketEvent.id)
        )
        return list((await session.execute(stmt)).scalars())


async def list_page(
    *,
    tenant_id: str,
    requester_id: str | None,
    assignee_id: str | None,
    status: str | None,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[Ticket]:
    """Newest first. `after` is the (created_at, id) of the last row of the previous page."""
    stmt = select(Ticket)
    if requester_id:
        stmt = stmt.where(Ticket.requester_id == requester_id)
    if assignee_id:
        stmt = stmt.where(Ticket.assignee_id == assignee_id)
    if status:
        stmt = stmt.where(Ticket.status == status)
    if after:
        stmt = stmt.where(tuple_(Ticket.created_at, Ticket.id) < tuple_(*after))
    stmt = stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit)
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        return list((await session.execute(stmt)).scalars())


async def apply_transition(
    *,
    tenant_id: str,
    ticket_id: str,
    expected_version: int,
    action: str,
    from_status: str,
    to_status: str,
    assignee_id: str | None,
    actor_id: str,
    note: str | None,
) -> Ticket | None:
    """Changes status and records the event in one transaction.

    The UPDATE only matches if the row is still at expected_version, so when two operators
    act on the same version exactly one wins. Returns None for the loser.
    """
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.version == expected_version)
            .values(status=to_status, assignee_id=assignee_id, version=Ticket.version + 1, updated_at=func.now())
            .returning(Ticket.id)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is None:
            await session.rollback()
            return None
        session.add(
            TicketEvent(
                tenant_id=tenant_id, ticket_id=ticket_id, action=action,
                from_status=from_status, to_status=to_status, actor_id=actor_id, note=note,
            )
        )
        await session.flush()
        ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
        await session.commit()
    return ticket
