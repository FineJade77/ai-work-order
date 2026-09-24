import uuid

from pydantic import BaseModel, Field


class AgentTask(BaseModel):
    """One user message to process. Written to the agent_tasks stream by app-service.

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
    content: str = Field(min_length=1, max_length=2000)
