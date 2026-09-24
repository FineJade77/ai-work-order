import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app import audit, events, ratelimit, store
from app.policy import allowed_tools
from app.redaction import redact
from app.registry import REGISTRY, ToolContext, ToolError, ToolSpec, validate_arguments
from app.validation import parse_tenant_id

logger = logging.getLogger("tool_gateway.calls")

# Outcomes that are a policy decision rather than a failure; audited as 'rejected'.
_REJECTED_CODES = frozenset(
    {
        "UNKNOWN_TOOL",
        "TOOL_NOT_ALLOWED",
        "INVALID_ARGUMENTS",
        "APPROVAL_REJECTED",
        "MISSING_IDEMPOTENCY_KEY",
        "IDEMPOTENCY_CONFLICT",
        "IN_PROGRESS",
    }
)


class CallerError(Exception):
    """Agent Runtime broke the gateway contract. Returned as an HTTP error, never shown to the model."""

    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CallOutcome:
    """A tool result for the model: success, pending_approval, or an error it can react to."""

    status: str
    call_id: str | None = None
    result: Any = None
    approval_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False

    @property
    def audit_status(self) -> str:
        if self.status != "error":
            return self.status
        if self.error_code == "RATE_LIMITED":
            return "rate_limited"
        return "rejected" if self.error_code in _REJECTED_CODES else "error"


@dataclass(frozen=True)
class ResolveOutcome:
    approval_id: str
    approval_status: str
    call: CallOutcome


def _error(code: str, message: str, *, call_id: str | None = None, replayed: bool = False) -> CallOutcome:
    return CallOutcome(status="error", call_id=call_id, error_code=code, error_message=message, replayed=replayed)


def require_tenant(tenant_id: str) -> None:
    try:
        parse_tenant_id(tenant_id)
    except ValueError as exc:
        raise CallerError(400, "INVALID_TENANT", str(exc)) from exc


def _request_hash(session_id: str, agent_role: str, tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"session_id": session_id, "agent_role": agent_role, "tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
        audit.write_audit(
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

    bindings = allowed_tools(tenant_id, agent_role)
    if tool_name not in bindings:
        return _error("TOOL_NOT_ALLOWED", f"role {agent_role!r} is not allowed to call {tool_name}")

    violation = validate_arguments(tool_name, arguments)
    if violation:
        return _error("INVALID_ARGUMENTS", violation)

    if spec.side_effect and not idempotency_key:
        raise CallerError(
            400, "MISSING_IDEMPOTENCY_KEY", f"{tool_name} has side effects; an Idempotency-Key header is required"
        )

    ctx = ToolContext(
        tenant_id=tenant_id, session_id=session_id, agent_role=agent_role, idempotency_key=idempotency_key
    )
    request_hash = _request_hash(session_id, agent_role, tool_name, arguments)

    # Replays are answered before the rate limiter, so retrying a finished call costs nothing.
    if idempotency_key:
        existing = store.find_by_idempotency_key(tenant_id, idempotency_key)
        if existing is not None:
            return _resume(existing, request_hash, spec, ctx)

    if not ratelimit.acquire(tenant_id, tool_name):
        return _error("RATE_LIMITED", "too many calls to this tool; retry later")

    claimed = store.claim(
        tenant_id=tenant_id,
        session_id=session_id,
        agent_role=agent_role,
        tool_name=tool_name,
        arguments=arguments,
        request_hash=request_hash,
        idempotency_key=idempotency_key,
        requires_approval=bindings[tool_name],
    )
    if claimed is None:
        # A concurrent request with the same key won the insert; answer from its record.
        existing = store.find_by_idempotency_key(tenant_id, idempotency_key)
        return _resume(existing, request_hash, spec, ctx)

    call_id, approval_id = claimed
    if approval_id is not None:
        return CallOutcome(status="pending_approval", call_id=call_id, approval_id=approval_id)
    return _execute(call_id, spec, ctx, arguments)


def _resume(existing: store.CallRecord, request_hash: str, spec: ToolSpec, ctx: ToolContext) -> CallOutcome:
    if existing.request_hash != request_hash:
        raise CallerError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different request")

    if existing.status == "success":
        return CallOutcome(status="success", call_id=existing.id, result=existing.result, replayed=True)
    if existing.status == "pending_approval":
        return CallOutcome(
            status="pending_approval", call_id=existing.id, approval_id=existing.approval_id, replayed=True
        )
    if existing.status == "rejected":
        return _error(
            existing.error_code or "APPROVAL_REJECTED",
            existing.error_message or "rejected by approver",
            call_id=existing.id,
            replayed=True,
        )

    # 'error' is retryable; 'running' may be orphaned by a crashed replica. reclaim() lets
    # exactly one retrier through, and only once a 'running' row has gone stale.
    if not ratelimit.acquire(ctx.tenant_id, spec.name):
        return _error("RATE_LIMITED", "too many calls to this tool; retry later", call_id=existing.id)
    if store.reclaim(ctx.tenant_id, existing.id):
        return _execute(existing.id, spec, ctx, existing.arguments)
    raise CallerError(409, "IN_PROGRESS", "a call with this Idempotency-Key is still executing; retry later")


def _execute(call_id: str, spec: ToolSpec, ctx: ToolContext, arguments: dict[str, Any]) -> CallOutcome:
    try:
        raw = spec.handler(ctx, arguments)
    except ToolError as exc:
        store.fail(ctx.tenant_id, call_id, exc.code, exc.message)
        return _error(exc.code, exc.message, call_id=call_id)
    except Exception:
        # Internal detail (SQL, stack, hostnames) must not reach the model or the caller.
        logger.exception("tool %s failed call_id=%s", spec.name, call_id)
        store.fail(ctx.tenant_id, call_id, "TOOL_ERROR", "tool execution failed")
        return _error("TOOL_ERROR", "tool execution failed", call_id=call_id)

    result = redact(raw, spec.sensitive_fields)
    store.complete(ctx.tenant_id, call_id, result)
    return CallOutcome(status="success", call_id=call_id, result=result)


def list_approvals(tenant_id: str, status: str) -> list[store.ApprovalRecord]:
    require_tenant(tenant_id)
    return store.list_approvals(tenant_id, status)


def resolve_approval(
    *, tenant_id: str, approval_id: str, approve: bool, resolver: str, reason: str | None
) -> ResolveOutcome:
    require_tenant(tenant_id)
    try:
        uuid.UUID(approval_id)
    except ValueError as exc:
        raise CallerError(404, "APPROVAL_NOT_FOUND", "no such approval") from exc

    start = time.monotonic()
    record = store.resolve_approval(
        tenant_id=tenant_id, approval_id=approval_id, approve=approve, resolver=resolver, reason=reason
    )
    if record is None:
        raise CallerError(409, "APPROVAL_NOT_PENDING", "approval does not exist or was already resolved")

    spec = REGISTRY.get(record.tool_name)
    if not approve:
        outcome = _error("APPROVAL_REJECTED", record.error_message or "rejected by approver", call_id=record.id)
    elif spec is None:
        store.fail(tenant_id, record.id, "UNKNOWN_TOOL", f"tool {record.tool_name} is no longer registered")
        outcome = _error("UNKNOWN_TOOL", f"tool {record.tool_name} is no longer registered", call_id=record.id)
    else:
        ctx = ToolContext(
            tenant_id=tenant_id,
            session_id=record.session_id,
            agent_role=record.agent_role,
            idempotency_key=record.idempotency_key,
        )
        outcome = _execute(record.id, spec, ctx, record.arguments)

    audit.write_audit(
        tenant_id=tenant_id,
        session_id=record.session_id,
        agent_role=record.agent_role,
        tool_name=record.tool_name,
        arguments=record.arguments,
        status=outcome.audit_status,
        error_code=outcome.error_code,
        call_id=record.id,
        duration_ms=int((time.monotonic() - start) * 1000),
    )
    events.publish_approval_resolved(
        tenant_id=tenant_id,
        session_id=record.session_id,
        call_id=record.id,
        approval_id=approval_id,
        decision="approved" if approve else "rejected",
    )
    return ResolveOutcome(
        approval_id=approval_id, approval_status="approved" if approve else "rejected", call=outcome
    )
