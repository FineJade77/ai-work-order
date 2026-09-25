import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_ticket_access
from app.core.security import CurrentUser, current_user
from app.schemas.tickets import Status, TransitionRequest
from app.services.ticket_access import TicketAccess

# Responses are ticket-service's views, passed through unchanged.
router = APIRouter(prefix="/tickets", tags=["tickets"])
User = Annotated[CurrentUser, Depends(current_user)]
Access = Annotated[TicketAccess, Depends(get_ticket_access)]


@router.get("")
async def list_tickets(
    user: User,
    access: Access,
    status: Status | None = None,
    assignee_id: uuid.UUID | None = None,
    requester_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict:
    """Employees get their own tickets whatever the filters say; handlers can filter the whole tenant."""
    return await access.list(
        user, status=status, assignee_id=assignee_id, requester_id=requester_id, limit=limit, cursor=cursor
    )


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, user: User, access: Access) -> dict:
    return await access.get(user, ticket_id)


@router.post("/{ticket_id}/transitions")
async def transition(ticket_id: uuid.UUID, req: TransitionRequest, user: User, access: Access) -> dict:
    return await access.transition(
        user, ticket_id, action=req.action, expected_version=req.expected_version, note=req.note
    )
