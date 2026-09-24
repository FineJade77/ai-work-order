from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Conversation, Ticket
from app.db.session import session_factory, tenant_scope


def create_idempotent(
    *, tenant_id: str, conversation_id: str, summary: str, category: str, idempotency_key: str
) -> tuple[str, bool] | None:
    """Returns (ticket_id, created). A retry with the same key returns the original ticket
    with created=False. Returns None if the conversation isn't visible to this tenant."""
    Session = session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        # RLS hides other tenants' conversations, so this also rejects cross-tenant references.
        if session.get(Conversation, conversation_id) is None:
            return None

        inserted_id = session.execute(
            pg_insert(Ticket)
            .values(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                category=category,
                summary=summary,
                status="open",
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "idempotency_key"],
                index_where=Ticket.idempotency_key.is_not(None),
            )
            .returning(Ticket.id)
        ).scalar_one_or_none()
        if inserted_id is not None:
            session.commit()
            return str(inserted_id), True

        # Same transaction as the insert: tenant_scope is transaction-local.
        existing_id = session.execute(
            select(Ticket.id).where(Ticket.idempotency_key == idempotency_key)
        ).scalar_one()
        session.commit()
        return str(existing_id), False
