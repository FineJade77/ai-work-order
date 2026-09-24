import base64
import json
import uuid
from datetime import datetime

from app.core.errors import ApiError
from app.core.tenancy import parse_tenant_id
from app.db.models import Ticket, TicketEvent
from app.domain import state_machine
from app.repositories import tickets as repo


def require_tenant(tenant_id: str) -> None:
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        raise ApiError(400, "INVALID_TENANT", str(exc)) from exc


def encode_cursor(ticket: Ticket) -> str:
    raw = json.dumps([ticket.created_at.isoformat(), str(ticket.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, ticket_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created_at), uuid.UUID(ticket_id)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "cursor is malformed") from exc


async def create_ticket(
    *, tenant_id: str, requester_id: str, conversation_id: str | None, category: str, summary: str, idempotency_key: str
) -> tuple[Ticket, bool]:
    require_tenant(tenant_id)
    return await repo.create_idempotent(
        tenant_id=tenant_id,
        requester_id=requester_id,
        conversation_id=conversation_id,
        category=category,
        summary=summary,
        idempotency_key=idempotency_key,
    )


async def get_ticket(tenant_id: str, ticket_id: str) -> tuple[Ticket, list[TicketEvent]]:
    require_tenant(tenant_id)
    ticket = await repo.get(tenant_id, ticket_id)
    if ticket is None:  # also the answer for another tenant's ticket: RLS hides it
        raise ApiError(404, "TICKET_NOT_FOUND", "ticket not found")
    return ticket, await repo.events(tenant_id, ticket_id)


async def list_tickets(
    *,
    tenant_id: str,
    requester_id: str | None,
    assignee_id: str | None,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Ticket], str | None]:
    require_tenant(tenant_id)
    # Fetch one extra row to learn whether another page exists without a COUNT query.
    rows = await repo.list_page(
        tenant_id=tenant_id,
        requester_id=requester_id,
        assignee_id=assignee_id,
        status=status,
        limit=limit + 1,
        after=decode_cursor(cursor) if cursor else None,
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit else None
    return page, next_cursor


async def transition(
    *,
    tenant_id: str,
    ticket_id: str,
    action: str,
    actor_id: str,
    expected_version: int,
    assignee_id: str | None,
    note: str | None,
) -> Ticket:
    require_tenant(tenant_id)
    ticket = await repo.get(tenant_id, ticket_id)
    if ticket is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "ticket not found")
    if ticket.version != expected_version:
        raise ApiError(409, "VERSION_CONFLICT", f"ticket is at version {ticket.version}, not {expected_version}")

    try:
        planned = state_machine.plan(
            action=action,
            current_status=ticket.status,
            actor_id=actor_id,
            requester_id=str(ticket.requester_id),
            current_assignee_id=str(ticket.assignee_id) if ticket.assignee_id else None,
            assignee_id=assignee_id,
            note=note,
        )
    except state_machine.TransitionError as exc:
        raise ApiError(exc.http_status, exc.code, exc.message) from exc

    updated = await repo.apply_transition(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        expected_version=expected_version,
        action=action,
        from_status=ticket.status,
        to_status=planned.to_status,
        assignee_id=planned.assignee_id,
        actor_id=actor_id,
        note=note,
    )
    if updated is None:  # someone else changed it between our read and our write
        raise ApiError(409, "VERSION_CONFLICT", "ticket was modified concurrently; reload and retry")
    return updated
