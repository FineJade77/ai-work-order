import logging

import redis

from app.redis_client import redis_client

logger = logging.getLogger("tool_gateway.events")

APPROVAL_EVENTS_STREAM = "tool_approval_events"


def publish_approval_resolved(
    *, tenant_id: str, session_id: str, call_id: str, approval_id: str, decision: str
) -> None:
    """Tells Agent Runtime a paused session can resume.

    The consumer re-issues the original /v1/tools/call with the same Idempotency-Key and
    gets the stored outcome, so this event carries identifiers only, never the result.
    """
    try:
        redis_client.xadd(
            APPROVAL_EVENTS_STREAM,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "call_id": call_id,
                "approval_id": approval_id,
                "decision": decision,
            },
            maxlen=100_000,
            approximate=True,
        )
    except redis.RedisError:
        # The decision is already durable in PG; a lost event delays the resume, it doesn't
        # lose the outcome. Agent Runtime can recover by polling pending sessions.
        logger.exception("failed to publish approval event approval_id=%s", approval_id)
