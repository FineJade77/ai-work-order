import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    created_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationView]


class MessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    task_id: uuid.UUID | None
    created_at: datetime


class History(BaseModel):
    messages: list[MessageView]
    # The turn still being answered, if any: reattach to it with GET .../stream?task_id=
    active_task_id: uuid.UUID | None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class SendMessageResponse(BaseModel):
    task_id: uuid.UUID
    message_id: uuid.UUID
