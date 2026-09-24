import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.core.security import require_service_token
from app.schemas.tickets import (
    CreateTicketRequest,
    CreateTicketResponse,
    Status,
    TicketDetail,
    TicketEventView,
    TicketPage,
    TicketView,
    TransitionRequest,
)
from app.services import ticket_service

router = APIRouter(prefix="/tickets", tags=["tickets"], dependencies=[Depends(require_service_token)])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_ticket(
    req: CreateTicketRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CreateTicketResponse:
    ticket, created = ticket_service.create_ticket(
        tenant_id=req.tenant_id,
        requester_id=str(req.requester_id),
        conversation_id=str(req.conversation_id) if req.conversation_id else None,
        category=req.category,
        summary=req.summary,
        idempotency_key=idempotency_key,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return CreateTicketResponse(ticket=TicketView.model_validate(ticket), created=created)


@router.get("/{ticket_id}")
def get_ticket(ticket_id: uuid.UUID, tenant_id: str) -> TicketDetail:
    ticket, events = ticket_service.get_ticket(tenant_id, str(ticket_id))
    return TicketDetail(
        ticket=TicketView.model_validate(ticket),
        events=[TicketEventView.model_validate(e) for e in events],
    )


@router.get("")
def list_tickets(
    tenant_id: str,
    requester_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    status_filter: Annotated[Status | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> TicketPage:
    items, next_cursor = ticket_service.list_tickets(
        tenant_id=tenant_id,
        requester_id=str(requester_id) if requester_id else None,
        assignee_id=str(assignee_id) if assignee_id else None,
        status=status_filter,
        limit=limit,
        cursor=cursor,
    )
    return TicketPage(items=[TicketView.model_validate(t) for t in items], next_cursor=next_cursor)


@router.post("/{ticket_id}/transitions")
def transition(ticket_id: uuid.UUID, req: TransitionRequest) -> TicketView:
    ticket = ticket_service.transition(
        tenant_id=req.tenant_id,
        ticket_id=str(ticket_id),
        action=req.action,
        actor_id=str(req.actor_id),
        expected_version=req.expected_version,
        assignee_id=str(req.assignee_id) if req.assignee_id else None,
        note=req.note,
    )
    return TicketView.model_validate(ticket)
