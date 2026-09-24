import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.state_machine import ACTIONS, STATUSES

Action = Literal[ACTIONS]  # type: ignore[valid-type]
Status = Literal[STATUSES]  # type: ignore[valid-type]


class CreateTicketRequest(BaseModel):
    tenant_id: str
    requester_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    category: str = Field(min_length=1, max_length=50)
    summary: str = Field(min_length=1, max_length=500)


class TransitionRequest(BaseModel):
    tenant_id: str
    action: Action
    actor_id: uuid.UUID
    expected_version: int = Field(ge=1)
    assignee_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=1000)


class TicketView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    requester_id: uuid.UUID
    conversation_id: uuid.UUID | None
    category: str
    summary: str
    status: Status
    assignee_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


class TicketEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    from_status: str | None
    to_status: str
    actor_id: uuid.UUID
    note: str | None
    created_at: datetime


class CreateTicketResponse(BaseModel):
    ticket: TicketView
    created: bool


class TicketDetail(BaseModel):
    ticket: TicketView
    events: list[TicketEventView]


class TicketPage(BaseModel):
    items: list[TicketView]
    next_cursor: str | None
