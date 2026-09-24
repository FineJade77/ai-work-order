import json
import logging
from typing import Any

from shared.db.models import ToolCallLog
from shared.db.session import app_session_factory, tenant_scope

from app.redaction import redact

logger = logging.getLogger("tool_gateway.audit")

PARAMS_SUMMARY_MAX_CHARS = 2000


def write_audit(
    *,
    tenant_id: str,
    session_id: str | None,
    agent_role: str | None,
    tool_name: str,
    arguments: Any,
    status: str,
    error_code: str | None,
    call_id: str | None,
    duration_ms: int,
) -> None:
    """One row per attempt, including replays and rejections; tool_calls holds the deduplicated state."""
    params_summary = json.dumps(redact(arguments), ensure_ascii=False)[:PARAMS_SUMMARY_MAX_CHARS]
    try:
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            session.add(
                ToolCallLog(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    agent_role=agent_role,
                    call_id=call_id,
                    tool_name=tool_name,
                    params_summary=params_summary,
                    status=status,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to write tool_call_logs row tool=%s tenant_id=%s", tool_name, tenant_id)


def log_unscoped_rejection(*, tenant_id: str, tool_name: str, reason: str) -> None:
    """For requests with no valid tenant: RLS can't accept a row without a tenant context."""
    logger.warning("tool_call rejected tool=%s tenant_id=%r reason=%s", tool_name, tenant_id, reason)
