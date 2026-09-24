from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["start", "resolve", "close", "reopen", "cancel"]
Status = Literal["open", "in_progress", "resolved", "closed"]


class TransitionRequest(BaseModel):
    # No actor_id or assignee_id: the actor is the caller, and start assigns the caller.
    action: Action
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)
