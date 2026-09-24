from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ToolError(Exception):
    """A business-level failure the model should see (vs. an internal crash, which is masked)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolContext:
    """Values supplied by Agent Runtime, never by the model's tool arguments."""

    tenant_id: str
    session_id: str
    agent_role: str
    idempotency_key: str | None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], Any]
    # Side-effecting tools must be called with an Idempotency-Key and are recorded in tool_calls.
    side_effect: bool

    def to_openai_tool(self) -> dict[str, Any]:
        # Only what the model needs. Handler, downstream endpoints and credentials stay server-side.
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }
