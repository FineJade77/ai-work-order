from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: str


class GenerateRequest(BaseModel):
    tenant_id: str
    system_prompt: str = ""
    messages: list[Message]
