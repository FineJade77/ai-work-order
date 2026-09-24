from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.db.models import ToolApproval, ToolCall
from shared.db.session import app_session_factory, tenant_scope

# A 'running' row older than this is presumed orphaned by a crashed gateway replica and
# may be reclaimed by a retry. Must exceed the slowest tool's expected execution time.
STALE_RUNNING_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class CallRecord:
    id: str
    session_id: str
    agent_role: str
    tool_name: str
    arguments: dict[str, Any]
    request_hash: str
    idempotency_key: str | None
    status: str
    result: Any
    error_code: str | None
    error_message: str | None
    approval_id: str | None


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    call_id: str
    session_id: str
    agent_role: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    resolver: str | None
    reason: str | None
    requested_at: datetime
    resolved_at: datetime | None


def _call_record(call: ToolCall, approval_id: Any) -> CallRecord:
    return CallRecord(
        id=str(call.id),
        session_id=call.session_id,
        agent_role=call.agent_role,
        tool_name=call.tool_name,
        arguments=call.arguments,
        request_hash=call.request_hash,
        idempotency_key=call.idempotency_key,
        status=call.status,
        result=call.result,
        error_code=call.error_code,
        error_message=call.error_message,
        approval_id=str(approval_id) if approval_id else None,
    )


def find_by_idempotency_key(tenant_id: str, idempotency_key: str) -> CallRecord | None:
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        row = session.execute(
            select(ToolCall, ToolApproval.id)
            .outerjoin(ToolApproval, ToolApproval.tool_call_id == ToolCall.id)
            .where(ToolCall.idempotency_key == idempotency_key)
        ).first()
    return _call_record(*row) if row else None


def claim(
    *,
    tenant_id: str,
    session_id: str,
    agent_role: str,
    tool_name: str,
    arguments: dict[str, Any],
    request_hash: str,
    idempotency_key: str | None,
    requires_approval: bool,
) -> tuple[str, str | None] | None:
    """Records a new call. Returns (call_id, approval_id), or None if another request already holds this key."""
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        call_id = session.execute(
            pg_insert(ToolCall)
            .values(
                tenant_id=tenant_id,
                session_id=session_id,
                agent_role=agent_role,
                tool_name=tool_name,
                arguments=arguments,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                status="pending_approval" if requires_approval else "running",
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "idempotency_key"],
                index_where=ToolCall.idempotency_key.is_not(None),
            )
            .returning(ToolCall.id)
        ).scalar_one_or_none()
        if call_id is None:
            session.rollback()
            return None

        approval_id = None
        if requires_approval:
            approval_id = session.execute(
                pg_insert(ToolApproval).values(tenant_id=tenant_id, tool_call_id=call_id).returning(ToolApproval.id)
            ).scalar_one()
        session.commit()
    return str(call_id), (str(approval_id) if approval_id else None)


def reclaim(tenant_id: str, call_id: str) -> bool:
    """Moves a failed or orphaned call back to 'running'. Only one concurrent retrier can win."""
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        claimed = session.execute(
            update(ToolCall)
            .where(ToolCall.id == call_id)
            .where(
                or_(
                    ToolCall.status == "error",
                    and_(ToolCall.status == "running", ToolCall.updated_at < func.now() - STALE_RUNNING_AFTER),
                )
            )
            .values(status="running", error_code=None, error_message=None, updated_at=func.now())
            .returning(ToolCall.id)
        ).scalar_one_or_none()
        session.commit()
    return claimed is not None


def complete(tenant_id: str, call_id: str, result: Any) -> None:
    _update_call(tenant_id, call_id, status="success", result=result)


def fail(tenant_id: str, call_id: str, code: str, message: str) -> None:
    _update_call(tenant_id, call_id, status="error", error_code=code, error_message=message)


def _update_call(tenant_id: str, call_id: str, **values: Any) -> None:
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        session.execute(update(ToolCall).where(ToolCall.id == call_id).values(updated_at=func.now(), **values))
        session.commit()


def list_approvals(tenant_id: str, status: str) -> list[ApprovalRecord]:
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        rows = session.execute(
            select(ToolApproval, ToolCall)
            .join(ToolCall, ToolCall.id == ToolApproval.tool_call_id)
            .where(ToolApproval.status == status)
            .order_by(ToolApproval.requested_at)
        ).all()
    return [
        ApprovalRecord(
            approval_id=str(approval.id),
            call_id=str(call.id),
            session_id=call.session_id,
            agent_role=call.agent_role,
            tool_name=call.tool_name,
            arguments=call.arguments,
            status=approval.status,
            resolver=approval.resolver,
            reason=approval.reason,
            requested_at=approval.requested_at,
            resolved_at=approval.resolved_at,
        )
        for approval, call in rows
    ]


def resolve_approval(
    *, tenant_id: str, approval_id: str, approve: bool, resolver: str, reason: str | None
) -> CallRecord | None:
    """Flips a pending approval and its call in one transaction. None if it wasn't pending."""
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        call_id = session.execute(
            update(ToolApproval)
            .where(ToolApproval.id == approval_id, ToolApproval.status == "pending")
            .values(
                status="approved" if approve else "rejected",
                resolver=resolver,
                reason=reason,
                resolved_at=func.now(),
            )
            .returning(ToolApproval.tool_call_id)
        ).scalar_one_or_none()
        if call_id is None:
            session.rollback()
            return None

        call_values: dict[str, Any] = (
            {"status": "running"}
            if approve
            else {
                "status": "rejected",
                "error_code": "APPROVAL_REJECTED",
                "error_message": reason or "rejected by approver",
            }
        )
        session.execute(update(ToolCall).where(ToolCall.id == call_id).values(updated_at=func.now(), **call_values))
        call = session.execute(select(ToolCall).where(ToolCall.id == call_id)).scalar_one()
        session.commit()
    return _call_record(call, approval_id)
