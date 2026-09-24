"""Idempotency records for side-effecting tool calls.

Each function runs its own short transaction on purpose. A claim must be committed before
the tool executes so that concurrent requests with the same key see it; folding these steps
into one request-scoped transaction would break that guarantee.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import ToolCall
from app.db.session import session_factory, tenant_scope

# A 'running' row older than this is presumed orphaned by a crashed gateway replica and
# may be reclaimed by a retry. Must exceed the slowest tool's expected execution time.
STALE_RUNNING_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class CallRecord:
    id: str
    tool_name: str
    arguments: dict[str, Any]
    request_hash: str
    status: str
    result: Any
    error_code: str | None
    error_message: str | None


def _record(call: ToolCall) -> CallRecord:
    return CallRecord(
        id=str(call.id),
        tool_name=call.tool_name,
        arguments=call.arguments,
        request_hash=call.request_hash,
        status=call.status,
        result=call.result,
        error_code=call.error_code,
        error_message=call.error_message,
    )


async def find_by_idempotency_key(tenant_id: str, idempotency_key: str) -> CallRecord | None:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        call = (
            await session.execute(select(ToolCall).where(ToolCall.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
    return _record(call) if call else None


async def claim(
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    agent_role: str,
    tool_name: str,
    arguments: dict[str, Any],
    request_hash: str,
    idempotency_key: str,
) -> str | None:
    """Records a new 'running' call. Returns its id, or None if another request already holds the key."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        call_id = (
            await session.execute(
                pg_insert(ToolCall)
                .values(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    agent_role=agent_role,
                    tool_name=tool_name,
                    arguments=arguments,
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    status="running",
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "idempotency_key"],
                    index_where=ToolCall.idempotency_key.is_not(None),
                )
                .returning(ToolCall.id)
            )
        ).scalar_one_or_none()
        await session.commit()
    return str(call_id) if call_id else None


async def reclaim(tenant_id: str, call_id: str) -> bool:
    """Moves a failed or orphaned call back to 'running'. Only one concurrent retrier can win."""
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        claimed = (
            await session.execute(
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
            )
        ).scalar_one_or_none()
        await session.commit()
    return claimed is not None


async def complete(tenant_id: str, call_id: str, result: Any) -> None:
    await _update(tenant_id, call_id, status="success", result=result)


async def fail(tenant_id: str, call_id: str, code: str, message: str) -> None:
    await _update(tenant_id, call_id, status="error", error_code=code, error_message=message)


async def _update(tenant_id: str, call_id: str, **values: Any) -> None:
    Session = session_factory()
    async with Session() as session, tenant_scope(session, tenant_id):
        await session.execute(update(ToolCall).where(ToolCall.id == call_id).values(updated_at=func.now(), **values))
        await session.commit()
