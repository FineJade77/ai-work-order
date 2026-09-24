from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.calls import CallOutcome


class ToolListResponse(BaseModel):
    tools: list[dict[str, Any]]


class ToolCallRequest(BaseModel):
    tenant_id: str
    session_id: str = Field(min_length=1)
    agent_role: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolErrorBody(BaseModel):
    code: str
    message: str


class ToolCallResponse(BaseModel):
    status: Literal["success", "pending_approval", "error"]
    call_id: str | None = None
    result: Any = None
    approval_id: str | None = None
    error: ToolErrorBody | None = None
    replayed: bool = False

    @classmethod
    def from_outcome(cls, outcome: CallOutcome) -> "ToolCallResponse":
        error = (
            ToolErrorBody(code=outcome.error_code, message=outcome.error_message or "")
            if outcome.status == "error" and outcome.error_code
            else None
        )
        return cls(
            status=outcome.status,
            call_id=outcome.call_id,
            result=outcome.result,
            approval_id=outcome.approval_id,
            error=error,
            replayed=outcome.replayed,
        )


class ApprovalView(BaseModel):
    approval_id: str
    call_id: str
    session_id: str
    agent_role: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    resolver: str | None
    reason: str | None
    requested_at: datetime
    resolved_at: datetime | None


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalView]


class ResolveApprovalRequest(BaseModel):
    tenant_id: str
    decision: Literal["approve", "reject"]
    resolver: str = Field(min_length=1)
    reason: str | None = None


class ResolveApprovalResponse(BaseModel):
    approval_id: str
    approval_status: Literal["approved", "rejected"]
    call: ToolCallResponse
