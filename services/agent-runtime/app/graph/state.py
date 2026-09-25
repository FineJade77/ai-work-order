import operator
from dataclasses import dataclass
from typing import Annotated, Protocol, TypedDict

from app.clients.ai_gateway import AIGatewayClient
from app.clients.tool_gateway import ToolGatewayClient
from app.schemas.tasks import AgentTask


class AgentState(TypedDict, total=False):
    """Checkpointed per conversation (thread_id = conversation_id), so it carries the history."""

    # The task whose run last advanced this thread; tells a redelivered task whether to resume.
    task_id: str
    # That task's seq; a task with a lower one is a stale copy and must not touch the thread.
    seq: int
    # {"role": "user" | "assistant", "content": str}
    messages: Annotated[list[dict], operator.add]
    intent: str | None  # in_scope | out_of_scope | ticket_request | chitchat
    category: str | None  # vpn | account | email | other (create_ticket's category enum)
    knowledge: list[dict] | None
    # Set when the search itself failed (vs. succeeding with no hits); the code of the last failure.
    knowledge_error: str | None
    resolution: str | None  # resolved | unresolved | new_question
    ticket_summary: str | None
    ticket_id: str | None
    # resolved | replied | ticket_created | knowledge_unavailable; awaiting_feedback is the interrupt,
    # and budget_exceeded is reported by the runner without reaching the graph's state
    outcome: str | None


# Per-run fields, cleared when a new run starts on a thread whose previous run has ended.
RUN_FIELDS_RESET: dict = {
    "intent": None,
    "category": None,
    "knowledge": None,
    "knowledge_error": None,
    "resolution": None,
    "ticket_summary": None,
    "ticket_id": None,
    "outcome": None,
}


class Events(Protocol):
    async def publish(self, type_: str, **data: object) -> None: ...

    async def finish(self, *, outcome: str, content: str, ticket_id: str | None) -> None: ...


@dataclass(frozen=True)
class Deps:
    """Run context (LangGraph context_schema): not checkpointed, supplied again on every run."""

    task: AgentTask
    tools: ToolGatewayClient
    models: AIGatewayClient
    events: Events
