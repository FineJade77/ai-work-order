from pydantic import BaseModel


class KnowledgeResult(BaseModel):
    question: str
    answer: str
    score: float


class CreateTicketResult(BaseModel):
    ticket_id: str


class UpdateTicketStatusResult(BaseModel):
    ok: bool
