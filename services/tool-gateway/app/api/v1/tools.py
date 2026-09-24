from typing import Annotated

from fastapi import APIRouter, Depends, Header

from app.core.security import require_service_token
from app.schemas.tools import ToolCallRequest, ToolCallResponse, ToolErrorBody, ToolListResponse
from app.services import tool_call_service
from app.services.tool_call_service import CallOutcome

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(require_service_token)])


@router.get("/list")
def list_tools(tenant_id: str, agent_role: str) -> ToolListResponse:
    return ToolListResponse(tools=tool_call_service.list_tools(tenant_id, agent_role))


@router.post("/call")
def call_tool(
    req: ToolCallRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ToolCallResponse:
    outcome = tool_call_service.call_tool(
        tenant_id=req.tenant_id,
        session_id=req.session_id,
        agent_role=req.agent_role,
        tool_name=req.tool_name,
        arguments=req.arguments,
        idempotency_key=idempotency_key,
    )
    return _to_response(outcome)


def _to_response(outcome: CallOutcome) -> ToolCallResponse:
    error = (
        ToolErrorBody(code=outcome.error_code, message=outcome.error_message or "")
        if outcome.status == "error" and outcome.error_code
        else None
    )
    return ToolCallResponse(
        status=outcome.status,
        call_id=outcome.call_id,
        result=outcome.result,
        error=error,
        replayed=outcome.replayed,
    )
