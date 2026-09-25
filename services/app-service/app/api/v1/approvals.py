import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import get_approval_access
from app.core.security import CurrentUser, current_user
from app.services.approval_access import ApprovalAccess

# Responses are tool-gateway's views, passed through unchanged.
router = APIRouter(prefix="/approvals", tags=["approvals"])
User = Annotated[CurrentUser, Depends(current_user)]
Access = Annotated[ApprovalAccess, Depends(get_approval_access)]


class DecisionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@router.get("")
async def list_approvals(
    user: User,
    access: Access,
    status: Literal["pending", "approved", "rejected", "expired"] | None = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    """Handlers: the tenant's approvals (pending by default). Employees: their own requests."""
    return await access.list(user, status=status, limit=limit)


@router.get("/{approval_id}")
async def get_approval(approval_id: uuid.UUID, user: User, access: Access) -> dict:
    return await access.get(user, approval_id)


@router.post("/{approval_id}/approve")
async def approve(approval_id: uuid.UUID, req: DecisionRequest, user: User, access: Access) -> dict:
    return await access.decide(user, approval_id, decision="approve", note=req.note)


@router.post("/{approval_id}/reject")
async def reject(approval_id: uuid.UUID, req: DecisionRequest, user: User, access: Access) -> dict:
    return await access.decide(user, approval_id, decision="reject", note=req.note)
