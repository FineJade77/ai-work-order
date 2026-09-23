import logging
import time
from contextlib import contextmanager

from shared.db.models import ToolCallLog
from shared.db.session import app_session_factory, tenant_scope

logger = logging.getLogger("tool_gateway.audit")


@contextmanager
def audit_tool_call(tool_name: str, tenant_id: str, params_summary: str):
    """Times a tool call and writes one tool_call_logs row, regardless of outcome."""
    start = time.monotonic()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        _record(tool_name, tenant_id, params_summary, status, duration_ms)


def record_rejected(tool_name: str, tenant_id: str, params_summary: str, reason: str) -> None:
    """For calls rejected before a tenant context could be established (bad/missing tenant_id).

    Can't write to tool_call_logs here: RLS requires a valid app.tenant_id to satisfy the
    table's WITH CHECK policy, which a rejected call by definition doesn't have.
    """
    logger.warning(
        "tool_call rejected tool=%s tenant_id=%r reason=%s params=%s",
        tool_name,
        tenant_id,
        reason,
        params_summary,
    )


def _record(tool_name: str, tenant_id: str, params_summary: str, status: str, duration_ms: int) -> None:
    try:
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            session.add(
                ToolCallLog(
                    tenant_id=tenant_id,
                    tool_name=tool_name,
                    params_summary=params_summary,
                    status=status,
                    duration_ms=duration_ms,
                )
            )
            session.commit()
    except Exception:
        logger.exception(
            "failed to write tool_call_logs row tool=%s tenant_id=%s status=%s", tool_name, tenant_id, status
        )
