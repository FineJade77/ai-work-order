from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import calls
from app.auth import require_service_token
from app.calls import CallerError
from app.policy import allowed_tools
from app.redaction import redact
from app.registry import REGISTRY
from app.schemas import (
    ApprovalListResponse,
    ApprovalView,
    ResolveApprovalRequest,
    ResolveApprovalResponse,
    ToolCallRequest,
    ToolCallResponse,
    ToolListResponse,
)

app = FastAPI(title="tool-gateway")
v1 = [Depends(require_service_token)]


@app.exception_handler(CallerError)
async def caller_error_handler(_: Request, exc: CallerError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/tools/list", dependencies=v1)
def list_tools(tenant_id: str, agent_role: str) -> ToolListResponse:
    calls.require_tenant(tenant_id)
    bindings = allowed_tools(tenant_id, agent_role)
    return ToolListResponse(tools=[REGISTRY[name].to_openai_tool() for name in sorted(bindings)])


@app.post("/v1/tools/call", dependencies=v1)
def call_tool(
    req: ToolCallRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ToolCallResponse:
    outcome = calls.call_tool(
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        agent_role=req.agent_role,
        tool_name=req.tool_name,
        arguments=req.arguments,
        idempotency_key=idempotency_key,
    )
    return ToolCallResponse.from_outcome(outcome)


@app.get("/v1/approvals", dependencies=v1)
def list_approvals(
    tenant_id: str, status: Literal["pending", "approved", "rejected"] = "pending"
) -> ApprovalListResponse:
    records = calls.list_approvals(tenant_id, status)
    return ApprovalListResponse(
        approvals=[
            ApprovalView(
                approval_id=r.approval_id,
                call_id=r.call_id,
                session_id=r.session_id,
                agent_role=r.agent_role,
                tool_name=r.tool_name,
                arguments=redact(r.arguments),
                status=r.status,
                resolver=r.resolver,
                reason=r.reason,
                requested_at=r.requested_at,
                resolved_at=r.resolved_at,
            )
            for r in records
        ]
    )


@app.post("/v1/approvals/{approval_id}/resolve", dependencies=v1)
def resolve_approval(approval_id: str, req: ResolveApprovalRequest) -> ResolveApprovalResponse:
    outcome = calls.resolve_approval(
        tenant_id=req.tenant_id,
        approval_id=approval_id,
        approve=req.decision == "approve",
        resolver=req.resolver,
        reason=req.reason,
    )
    return ResolveApprovalResponse(
        approval_id=outcome.approval_id,
        approval_status=outcome.approval_status,
        call=ToolCallResponse.from_outcome(outcome.call),
    )
