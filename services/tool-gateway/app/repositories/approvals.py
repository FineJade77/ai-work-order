"""Approvals of side-effecting tool calls (HITL). Each function is one short transaction, like
tool_calls.py: state changes must be committed before anyone acts on them.

An approval moves pending -> approved | rejected | expired, exactly once (conditional UPDATEs).
Every decision is written together with an outbox row, so the event announcing it cannot be lost
between the commit and the publish: the relay (services/approval_relay.py) publishes it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from app.db.models import ApprovalOutbox, ToolApproval, ToolCall
from app.db.session import session_factory, tenant_scope


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    session_id: str
    requester_id: str
    agent_role: str
    status: str
    traceparent: str | None
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


def _record(a: ToolApproval) -> ApprovalRecord:
    return ApprovalRecord(
        id=str(a.id), call_id=str(a.call_id), tool_name=a.tool_name, arguments=a.arguments,
        session_id=a.session_id, requester_id=str(a.requester_id), agent_role=a.agent_role, status=a.status,
        traceparent=a.traceparent, expires_at=a.expires_at, decided_by=str(a.decided_by) if a.decided_by else None,
        decided_at=a.decided_at, note=a.note, created_at=a.created_at,
    )


async def open_for_call(
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    agent_role: str,
    tool_name: str,
    arguments: dict[str, Any],
    request_hash: str,
    idempotency_key: str,
    traceparent: str | None,
    ttl: timedelta,
) -> tuple[str, ApprovalRecord] | None:
    """Records the call as waiting for approval, its approval, and the check that expires it,
    in one transaction. Returns (call_id, approval), or None if the key is already taken."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        call_id = (
            await session.execute(
                pg_insert(ToolCall)
                .values(
                    tenant_id=tenant_id, session_id=session_id, user_id=user_id, agent_role=agent_role,
                    tool_name=tool_name, arguments=arguments, request_hash=request_hash,
                    idempotency_key=idempotency_key, status="pending_approval",
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "idempotency_key"], index_where=ToolCall.idempotency_key.is_not(None)
                )
                .returning(ToolCall.id)
            )
        ).scalar_one_or_none()
        if call_id is None:
            return None
        approval = ToolApproval(
            tenant_id=tenant_id, call_id=call_id, tool_name=tool_name, arguments=arguments, session_id=session_id,
            requester_id=user_id, agent_role=agent_role, traceparent=traceparent,
            expires_at=func.now() + ttl,
        )
        session.add(approval)
        await session.flush()
        await session.refresh(approval)
        session.add(
            ApprovalOutbox(tenant_id=tenant_id, approval_id=approval.id, kind="expire", due_at=approval.expires_at)
        )
        await session.commit()
    return str(call_id), _record(approval)


async def find_by_call(tenant_id: str, call_id: str) -> ApprovalRecord | None:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        approval = await session.scalar(select(ToolApproval).where(ToolApproval.call_id == call_id))
    return _record(approval) if approval else None


async def get(tenant_id: str, approval_id: str) -> ApprovalRecord | None:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        approval = await session.get(ToolApproval, approval_id)
    return _record(approval) if approval else None


async def list_page(
    tenant_id: str, *, status: str | None, requester_id: str | None, limit: int
) -> list[ApprovalRecord]:
    stmt = select(ToolApproval).order_by(ToolApproval.created_at.desc(), ToolApproval.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(ToolApproval.status == status)
    if requester_id:
        stmt = stmt.where(ToolApproval.requester_id == requester_id)
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        return [_record(a) for a in (await session.execute(stmt)).scalars()]


async def decide(tenant_id: str, approval_id: str, *, status: str, decided_by: str | None, note: str | None) -> bool:
    """pending -> status, only if still pending and not past its expiry; with the outbox row that
    publishes the decision. False if it was not pending (or had expired) any more."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        stmt = update(ToolApproval).where(ToolApproval.id == approval_id, ToolApproval.status == "pending")
        if status != "expired":  # an approver cannot decide what has already run out
            stmt = stmt.where(ToolApproval.expires_at > func.now())
        decided = (
            await session.execute(
                stmt.values(status=status, decided_by=decided_by, decided_at=func.now(), note=note)
                .returning(ToolApproval.id)
            )
        ).scalar_one_or_none()
        if decided is None:
            await session.rollback()
            return False
        session.add(ApprovalOutbox(tenant_id=tenant_id, approval_id=approval_id, kind="publish"))
        # A handler's decision makes the expiry check moot: close it, so that "open expiry checks"
        # equals "pending approvals", which the relay counts across tenants for the pending gauges.
        # Not when expiring: that is the relay handling the check itself, holding its row lock.
        if status != "expired":
            await session.execute(
                update(ApprovalOutbox)
                .where(
                    ApprovalOutbox.approval_id == approval_id,
                    ApprovalOutbox.kind == "expire",
                    ApprovalOutbox.done_at.is_(None),
                )
                .values(done_at=func.now())
            )
        await session.commit()
    return True


async def start_execution(tenant_id: str, call_id: str) -> bool:
    """pending_approval -> running for an approved call. Only one caller wins."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        approved = exists().where(and_(ToolApproval.call_id == ToolCall.id, ToolApproval.status == "approved"))
        started = (
            await session.execute(
                update(ToolCall)
                .where(ToolCall.id == call_id, ToolCall.status == "pending_approval", approved)
                .values(status="running", updated_at=func.now())
                .returning(ToolCall.id)
            )
        ).scalar_one_or_none()
        await session.commit()
    return started is not None


async def close_call(tenant_id: str, call_id: str, code: str, message: str) -> None:
    """A rejected or expired call ends in error for good (see tool_call_service.FINAL_CODES)."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        await session.execute(
            update(ToolCall)
            .where(ToolCall.id == call_id, ToolCall.status == "pending_approval")
            .values(status="error", error_code=code, error_message=message, updated_at=func.now())
        )
        await session.commit()
