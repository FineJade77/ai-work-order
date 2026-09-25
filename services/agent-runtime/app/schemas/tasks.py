import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AgentTask(BaseModel):
    """One unit of work for a conversation, written to the agent_tasks stream by app-service:
    an employee's message (kind "message"), or a handler's decision on an approval the
    conversation is waiting for (kind "approval", which resumes the paused run).

    tenant_id and user_id are trusted runtime context: they go to the gateways as-is and are
    never taken from model output.
    """

    task_id: uuid.UUID
    # The turn's position in its conversation (1, 2, 3...). A task older than one the conversation
    # has already moved past is a stale copy (e.g. requeued by app-service) and is skipped.
    seq: int = Field(ge=1)
    conversation_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    kind: Literal["message", "approval"] = "message"
    content: str = Field(default="", max_length=2000)
    approval_id: uuid.UUID | None = None
    decision: Literal["approved", "rejected", "expired"] | None = None
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _complete_for_its_kind(self) -> "AgentTask":
        if self.kind == "message" and not self.content:
            raise ValueError("a message task needs content")
        if self.kind == "approval" and (self.approval_id is None or self.decision is None):
            raise ValueError("an approval task needs approval_id and decision")
        return self
