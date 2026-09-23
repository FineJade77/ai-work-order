from abc import ABC, abstractmethod

from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.db.models import Ticket
from shared.db.session import app_session_factory, tenant_scope


class TicketProvider(ABC):
    @abstractmethod
    def create(
        self, *, tenant_id: str, summary: str, category: str, conversation_ref: str, idempotency_key: str
    ) -> str: ...

    @abstractmethod
    def update_status(self, *, tenant_id: str, ticket_id: str, status: str) -> bool: ...


class InternalTicketProvider(TicketProvider):
    """Writes to this project's own tickets table.

    A future provider (Jira/ServiceNow/etc, see architecture-design.md 3.4) implements the
    same interface; which one is active is a per-tenant configuration choice, not a code change.
    """

    def create(
        self, *, tenant_id: str, summary: str, category: str, conversation_ref: str, idempotency_key: str
    ) -> str:
        """Retrying with the same idempotency_key returns the original ticket_id.

        Agent Runtime resumes from a LangGraph checkpoint after a crash (see
        architecture-design.md 3.3), which can replay a create_ticket call whose result
        was never durably recorded. Without this, that replay creates a duplicate ticket.
        The insert-or-return-existing below is one atomic statement, so concurrent
        retries with the same key can't race into two rows either.
        """
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            stmt = (
                pg_insert(Ticket)
                .values(
                    tenant_id=tenant_id,
                    conversation_id=conversation_ref,
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
            )
            # Stay in this one transaction until we have a final id: set_config(..., true)
            # is transaction-local, so committing before the fallback lookup below would
            # drop the tenant context RLS needs and the SELECT would see zero rows.
            inserted_id = session.execute(stmt).scalar_one_or_none()
            if inserted_id is not None:
                session.commit()
                return str(inserted_id)

            existing = (
                session.query(Ticket)
                .filter_by(tenant_id=tenant_id, idempotency_key=idempotency_key)
                .one()
            )
            session.commit()
            return str(existing.id)

    def update_status(self, *, tenant_id: str, ticket_id: str, status: str) -> bool:
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            ticket = session.get(Ticket, ticket_id)
            if ticket is None:
                return False
            ticket.status = status
            session.commit()
            return True


ticket_provider: TicketProvider = InternalTicketProvider()
