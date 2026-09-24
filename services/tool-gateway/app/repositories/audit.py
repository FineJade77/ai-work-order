import json
import logging
import uuid
from typing import Any

from app.core.redaction import redact_for_audit
from app.db.models import ToolCallLog
from app.db.session import session_factory, tenant_scope

logger = logging.getLogger("tool_gateway.audit")

PARAMS_SUMMARY_MAX_CHARS = 2000


async def write(
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    agent_role: str,
    tool_name: str,
    arguments: Any,
    status: str,
    error_code: str | None,
    call_id: str | None,
    duration_ms: int,
) -> None:
    """One row per attempt, including replays and rejections. Redaction happens here so no
    caller can store raw arguments by accident."""
    params_summary = json.dumps(redact_for_audit(arguments), ensure_ascii=False)[:PARAMS_SUMMARY_MAX_CHARS]
    try:
        Session = session_factory()
        async with Session() as session, tenant_scope(session, tenant_id):
            session.add(
                ToolCallLog(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=_uuid_or_none(user_id),
                    agent_role=agent_role,
                    call_id=call_id,
                    tool_name=tool_name,
                    params_summary=params_summary,
                    status=status,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("failed to write tool_call_logs row tool=%s tenant_id=%s", tool_name, tenant_id)


def log_unscoped_rejection(*, tenant_id: str, tool_name: str, reason: str) -> None:
    """For requests with no valid tenant: RLS can't accept a row without a tenant context."""
    logger.warning("tool_call rejected tool=%s tenant_id=%r reason=%s", tool_name, tenant_id, reason)


def _uuid_or_none(value: str) -> str | None:
    """A call rejected for a malformed user_id must still be audited; store NULL rather than fail."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
