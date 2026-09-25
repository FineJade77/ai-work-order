"""Deciding approvals (the handler's side of HITL). The agent's side is in tool_call_service:
a call that needs approval returns pending_approval and is called again once decided.

Roles are app-service's business: it only lets handlers through. Here: the approval must still be
pending and not past its expiry, and nobody approves their own request (four-eyes).
"""

from app.core.errors import CallerError
from app.core.redaction import mask_secret_fields
from app.core.tenancy import parse_tenant_id
from app.repositories import approvals
from app.schemas.approvals import ApprovalView


def view(a: approvals.ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        id=a.id, call_id=a.call_id, tool_name=a.tool_name, arguments=mask_secret_fields(a.arguments),
        conversation_id=a.session_id, requester_id=a.requester_id, status=a.status, expires_at=a.expires_at,
        decided_by=a.decided_by, decided_at=a.decided_at, note=a.note, created_at=a.created_at,
    )


def _tenant(tenant_id: str) -> None:
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        raise CallerError(400, "INVALID_TENANT", str(exc)) from exc


async def list_approvals(
    tenant_id: str, *, status: str | None, requester_id: str | None, limit: int
) -> list[ApprovalView]:
    _tenant(tenant_id)
    records = await approvals.list_page(tenant_id, status=status, requester_id=requester_id, limit=limit)
    return [view(a) for a in records]


async def get_approval(tenant_id: str, approval_id: str) -> ApprovalView:
    _tenant(tenant_id)
    approval = await approvals.get(tenant_id, approval_id)
    if approval is None:
        raise CallerError(404, "APPROVAL_NOT_FOUND", "approval not found")
    return view(approval)


async def decide(
    tenant_id: str, approval_id: str, *, decision: str, approver_id: str, note: str | None
) -> ApprovalView:
    _tenant(tenant_id)
    approval = await approvals.get(tenant_id, approval_id)
    if approval is None:
        raise CallerError(404, "APPROVAL_NOT_FOUND", "approval not found")
    if approval.requester_id == approver_id:
        raise CallerError(403, "SELF_APPROVAL", "an approval cannot be decided by the person who requested it")
    status = "approved" if decision == "approve" else "rejected"
    if not await approvals.decide(tenant_id, approval_id, status=status, decided_by=approver_id, note=note):
        current = await approvals.get(tenant_id, approval_id)
        if current.status == "pending":  # still pending, so it must be past its expiry
            raise CallerError(409, "APPROVAL_EXPIRED", "the approval expired before it was decided")
        raise CallerError(409, "APPROVAL_NOT_PENDING", f"the approval is already {current.status}")
    return view(await approvals.get(tenant_id, approval_id))
