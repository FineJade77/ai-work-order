from dataclasses import dataclass

from app.core.tenancy import parse_tenant_id
from app.repositories import tickets


class InvalidRequest(Exception):
    pass


class ConversationNotFound(Exception):
    pass


@dataclass(frozen=True)
class CreatedTicket:
    ticket_id: str
    created: bool


def create_ticket(
    *, tenant_id: str, conversation_id: str, summary: str, category: str, idempotency_key: str
) -> CreatedTicket:
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    result = tickets.create_idempotent(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        summary=summary,
        category=category,
        idempotency_key=idempotency_key,
    )
    if result is None:
        raise ConversationNotFound(conversation_id)
    ticket_id, created = result
    return CreatedTicket(ticket_id=ticket_id, created=created)
