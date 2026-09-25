import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ApprovalView(BaseModel):
    id: str
    call_id: str
    tool_name: str
    # Shown to the approver as the agent would send them; credential-like fields masked.
    arguments: dict[str, Any]
    conversation_id: str
    requester_id: str
    status: Literal["pending", "approved", "rejected", "expired"]
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


class ApprovalPage(BaseModel):
    items: list[ApprovalView]


class DecisionRequest(BaseModel):
    tenant_id: str
    decision: Literal["approve", "reject"]
    # The handler deciding (app-service checks the role); must not be the requester.
    approver_id: uuid.UUID
    note: str | None = Field(default=None, max_length=1000)
