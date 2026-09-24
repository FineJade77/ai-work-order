import uuid

from pydantic import BaseModel, Field


class CreateTicketRequest(BaseModel):
    tenant_id: str
    conversation_id: uuid.UUID
    summary: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1)


class CreateTicketResponse(BaseModel):
    ticket_id: str
    created: bool
