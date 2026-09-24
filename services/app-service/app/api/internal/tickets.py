from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.core.security import require_internal_token
from app.schemas.tickets import CreateTicketRequest, CreateTicketResponse
from app.services import ticket_service

router = APIRouter(prefix="/tickets", tags=["internal"], dependencies=[Depends(require_internal_token)])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_ticket(
    req: CreateTicketRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> CreateTicketResponse:
    try:
        ticket = ticket_service.create_ticket(
            tenant_id=req.tenant_id,
            conversation_id=str(req.conversation_id),
            summary=req.summary,
            category=req.category,
            idempotency_key=idempotency_key,
        )
    except ticket_service.InvalidRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ticket_service.ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc

    if not ticket.created:
        response.status_code = status.HTTP_200_OK
    return CreateTicketResponse(ticket_id=ticket.ticket_id, created=ticket.created)
