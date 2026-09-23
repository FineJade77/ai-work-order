import logging

from shared.db.models import ModelCallLog
from shared.db.session import app_session_factory, tenant_scope

logger = logging.getLogger("ai_gateway.usage")


def record_usage(
    *, tenant_id: str, model: str, prompt_tokens: int, completion_tokens: int, duration_ms: int, status: str
) -> None:
    try:
        Session = app_session_factory()
        with Session() as session, tenant_scope(session, tenant_id):
            session.add(
                ModelCallLog(
                    tenant_id=tenant_id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to write model_call_logs row tenant_id=%s model=%s", tenant_id, model)
