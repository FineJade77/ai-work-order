from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from app.knowledge import search_knowledge_impl
from app.ticket_provider import ticket_provider

TICKET_STATUSES = ["draft", "open", "in_progress", "resolved", "closed", "reopened"]
TICKET_CATEGORIES = ["vpn", "account", "email", "other"]
UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


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
    side_effect: bool
    sensitive_fields: frozenset[str] = frozenset()

    def to_openai_tool(self) -> dict[str, Any]:
        # Only what the model needs. Handler, downstream endpoints and credentials stay server-side.
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"results": search_knowledge_impl(query=args["query"], tenant_id=ctx.tenant_id)}


def _create_ticket(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # The gateway-level idempotency record covers ordinary retries. Passing the same key
    # down covers the remaining window: a crash after the ticket row is written but before
    # the gateway records success, which leaves the call reclaimable and re-executed.
    ticket_id = ticket_provider.create(
        tenant_id=ctx.tenant_id,
        summary=args["summary"],
        category=args["category"],
        conversation_ref=ctx.session_id,
        idempotency_key=ctx.idempotency_key,
    )
    return {"ticket_id": ticket_id}


def _update_ticket_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ok = ticket_provider.update_status(tenant_id=ctx.tenant_id, ticket_id=args["ticket_id"], status=args["status"])
    if not ok:
        raise ToolError("NOT_FOUND", f"ticket {args['ticket_id']} not found")
    return {"ticket_id": args["ticket_id"], "status": args["status"]}


_SPECS = [
    ToolSpec(
        name="search_knowledge",
        description="在企业 IT 知识库中检索与员工问题相关的问答条目。回答员工 IT 问题前应先调用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "员工问题的关键词或原句"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_search_knowledge,
        side_effect=False,
    ),
    ToolSpec(
        name="create_ticket",
        description="知识库方案无法解决员工问题时，为其创建一张 IT 工单。",
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "问题描述，包含已尝试过的知识库方案",
                },
                "category": {"type": "string", "enum": TICKET_CATEGORIES, "description": "问题类别"},
            },
            "required": ["summary", "category"],
            "additionalProperties": False,
        },
        handler=_create_ticket,
        side_effect=True,
    ),
    ToolSpec(
        name="update_ticket_status",
        description="更新一张已有工单的状态，例如员工确认问题已解决后关闭工单。",
        parameters={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "pattern": UUID_PATTERN, "description": "工单 ID"},
                "status": {"type": "string", "enum": TICKET_STATUSES, "description": "目标状态"},
            },
            "required": ["ticket_id", "status"],
            "additionalProperties": False,
        },
        handler=_update_ticket_status,
        side_effect=True,
    ),
]

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}

_VALIDATORS: dict[str, Draft202012Validator] = {}
for _spec in _SPECS:
    Draft202012Validator.check_schema(_spec.parameters)
    _VALIDATORS[_spec.name] = Draft202012Validator(_spec.parameters)


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Returns a model-readable description of the first schema violation, or None if valid."""
    errors = sorted(_VALIDATORS[tool_name].iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    first = errors[0]
    path = "/".join(str(p) for p in first.absolute_path) or "(root)"
    return f"{path}: {first.message}"
