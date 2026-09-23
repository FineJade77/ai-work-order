from abc import ABC, abstractmethod

from shared.db.models import Ticket
from shared.db.session import app_session_factory, tenant_scope


class TicketProvider(ABC):
    @abstractmethod
    def create(self, *, tenant_id: str, summary: str, category: str, conversation_ref: str) -> str: ...

    @abstractmethod
    def update_status(self, *, tenant_id: str, ticket_id: str, status: str) -> bool: ...


class InternalTicketProvider(TicketProvider):
    """Writes to this project's own tickets table.

    A future provider (Jira/ServiceNow/etc, see architecture-design.md 3.4) implements the
    same interface; which one is active is a per-tenant configuration choice, not a code change.
    """

    def create(self, *, tenant_id: str, summary: str, category: str, conversation_ref: str) -> str:
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            ticket = Ticket(
                tenant_id=tenant_id,
                conversation_id=conversation_ref,
                category=category,
                summary=summary,
                status="open",
            )
            session.add(ticket)
            session.commit()
            return str(ticket.id)

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
