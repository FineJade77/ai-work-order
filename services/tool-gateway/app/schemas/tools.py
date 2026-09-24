from typing import Any, Literal

from pydantic import BaseModel, Field


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
    status: Literal["success", "error"]
    call_id: str | None = None
    result: Any = None
    error: ToolErrorBody | None = None
    replayed: bool = False
