import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.core.errors import CallerError
from app.core.redaction import mask_secret_fields
from app.core.tenancy import parse_tenant_id
from app.repositories import audit, bindings, tool_calls
from app.tools.base import ToolContext, ToolError, ToolSpec
from app.tools.registry import REGISTRY, validate_arguments

logger = logging.getLogger("tool_gateway.service")

# Error codes that are a policy decision rather than a failure; audited as 'rejected'.
_REJECTED_CODES = frozenset(
    {
        "UNKNOWN_TOOL",
        "TOOL_NOT_ALLOWED",
        "INVALID_ARGUMENTS",
        "MISSING_IDEMPOTENCY_KEY",
        "IDEMPOTENCY_CONFLICT",
        "IN_PROGRESS",
    }
)


@dataclass(frozen=True)
class CallOutcome:
    """A tool result for the model: success, or an error it can react to."""

    status: str
    call_id: str | None = None
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False

    @property
    def audit_status(self) -> str:
        if self.status == "success":
            return "success"
        return "rejected" if self.error_code in _REJECTED_CODES else "error"


def _error(code: str, message: str, *, call_id: str | None = None, replayed: bool = False) -> CallOutcome:
    return CallOutcome(status="error", call_id=call_id, error_code=code, error_message=message, replayed=replayed)


def require_tenant(tenant_id: str) -> None:
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        raise CallerError(400, "INVALID_TENANT", str(exc)) from exc


def request_hash(session_id: str, agent_role: str, tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"session_id": session_id, "agent_role": agent_role, "tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def list_tools(tenant_id: str, agent_role: str) -> list[dict[str, Any]]:
    require_tenant(tenant_id)
    allowed = bindings.bound_tool_names(tenant_id, agent_role) & REGISTRY.keys()
    return [REGISTRY[name].to_openai_tool() for name in sorted(allowed)]


def call_tool(
    *,
    tenant_id: str,
    session_id: str,
    agent_role: str,
    tool_name: str,
    arguments: dict[str, Any],
    idempotency_key: str | None,
) -> CallOutcome:
    try:
        require_tenant(tenant_id)
    except CallerError as exc:
        audit.log_unscoped_rejection(tenant_id=tenant_id, tool_name=tool_name, reason=exc.message)
        raise

    start = time.monotonic()
    audit_status, error_code, call_id = "error", "INTERNAL", None
    try:
        outcome = _call_tool(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_role=agent_role,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )
        audit_status, error_code, call_id = outcome.audit_status, outcome.error_code, outcome.call_id
        return outcome
    except CallerError as exc:
        audit_status, error_code = "rejected", exc.code
        raise
    finally:
        audit.write(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_role=agent_role,
            tool_name=tool_name,
            arguments=arguments,
            status=audit_status,
            error_code=error_code,
            call_id=call_id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def _call_tool(
    *,
    tenant_id: str,
    session_id: str,
    agent_role: str,
    tool_name: str,
    arguments: dict[str, Any],
    idempotency_key: str | None,
) -> CallOutcome:
    spec = REGISTRY.get(tool_name)
    if spec is None:
        return _error("UNKNOWN_TOOL", f"unknown tool: {tool_name}")

    if tool_name not in bindings.bound_tool_names(tenant_id, agent_role):
        return _error("TOOL_NOT_ALLOWED", f"role {agent_role!r} is not allowed to call {tool_name}")

    violation = validate_arguments(tool_name, arguments)
    if violation:
        return _error("INVALID_ARGUMENTS", violation)

    ctx = ToolContext(
        tenant_id=tenant_id, session_id=session_id, agent_role=agent_role, idempotency_key=idempotency_key
    )

    # Read-only tools have nothing to deduplicate; skip the two extra transactions.
    if not spec.side_effect:
        try:
            return CallOutcome(status="success", result=_invoke(spec, ctx, arguments))
        except ToolError as exc:
            return _error(exc.code, exc.message)

    if not idempotency_key:
        raise CallerError(
            400, "MISSING_IDEMPOTENCY_KEY", f"{tool_name} has side effects; an Idempotency-Key header is required"
        )

    fingerprint = request_hash(session_id, agent_role, tool_name, arguments)
    existing = tool_calls.find_by_idempotency_key(tenant_id, idempotency_key)
    if existing is not None:
        return _resume(existing, fingerprint, spec, ctx)

    call_id = tool_calls.claim(
        tenant_id=tenant_id,
        session_id=session_id,
        agent_role=agent_role,
        tool_name=tool_name,
        arguments=arguments,
        request_hash=fingerprint,
        idempotency_key=idempotency_key,
    )
    if call_id is None:
        # A concurrent request with the same key won the insert; answer from its record.
        return _resume(tool_calls.find_by_idempotency_key(tenant_id, idempotency_key), fingerprint, spec, ctx)
    return _execute_recorded(call_id, spec, ctx, arguments)


def _resume(existing: tool_calls.CallRecord, fingerprint: str, spec: ToolSpec, ctx: ToolContext) -> CallOutcome:
    if existing.request_hash != fingerprint:
        raise CallerError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different request")
    if existing.status == "success":
        return CallOutcome(status="success", call_id=existing.id, result=existing.result, replayed=True)

    # 'error' is retryable; 'running' may be orphaned by a crashed replica. reclaim() lets
    # exactly one retrier through, and only once a 'running' row has gone stale.
    if tool_calls.reclaim(ctx.tenant_id, existing.id):
        return _execute_recorded(existing.id, spec, ctx, existing.arguments)
    raise CallerError(409, "IN_PROGRESS", "a call with this Idempotency-Key is still executing; retry later")


def _execute_recorded(call_id: str, spec: ToolSpec, ctx: ToolContext, arguments: dict[str, Any]) -> CallOutcome:
    try:
        result = _invoke(spec, ctx, arguments)
    except ToolError as exc:
        tool_calls.fail(ctx.tenant_id, call_id, exc.code, exc.message)
        return _error(exc.code, exc.message, call_id=call_id)
    tool_calls.complete(ctx.tenant_id, call_id, result)
    return CallOutcome(status="success", call_id=call_id, result=result)


def _invoke(spec: ToolSpec, ctx: ToolContext, arguments: dict[str, Any]) -> Any:
    """Runs the handler. Internal failures surface only as a generic TOOL_ERROR, so SQL,
    stack traces and hostnames never reach the model or the caller."""
    try:
        raw = spec.handler(ctx, arguments)
    except ToolError:
        raise
    except Exception as exc:
        logger.exception("tool %s failed session_id=%s", spec.name, ctx.session_id)
        raise ToolError("TOOL_ERROR", "tool execution failed") from exc
    return mask_secret_fields(raw)
