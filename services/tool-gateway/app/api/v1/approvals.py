import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.security import require_service_token
from app.schemas.approvals import ApprovalPage, ApprovalView, DecisionRequest
from app.services import approval_service

# Called by app-service on behalf of handlers (listing, deciding) and employees (their own).
router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(require_service_token)])


@router.get("")
async def list_approvals(
    tenant_id: str,
    status: Literal["pending", "approved", "rejected", "expired"] | None = None,
    requester_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ApprovalPage:
    items = await approval_service.list_approvals(
        tenant_id, status=status, requester_id=str(requester_id) if requester_id else None, limit=limit
    )
    return ApprovalPage(items=items)


@router.get("/{approval_id}")
async def get_approval(approval_id: uuid.UUID, tenant_id: str) -> ApprovalView:
    return await approval_service.get_approval(tenant_id, str(approval_id))


@router.post("/{approval_id}/decision")
async def decide(approval_id: uuid.UUID, req: DecisionRequest) -> ApprovalView:
    return await approval_service.decide(
        req.tenant_id, str(approval_id), decision=req.decision, approver_id=str(req.approver_id), note=req.note
    )
