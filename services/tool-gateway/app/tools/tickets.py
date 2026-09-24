from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import get_settings
from app.tools.base import ToolContext, ToolSpec

TICKET_CATEGORIES = ["vpn", "account", "email", "other"]


class TicketProvider(ABC):
    @abstractmethod
    def create(
        self, *, tenant_id: str, requester_id: str, summary: str, category: str, conversation_ref: str,
        idempotency_key: str,
    ) -> str: ...


class InternalTicketProvider(TicketProvider):
    """Creates tickets through ticket-service, which owns ticket data and lifecycle.

    Same shape as a future Jira/ServiceNow provider: an HTTP call to the system that owns
    tickets. The idempotency key is passed through, so ticket-service deduplicates too; that
    covers a crash after the ticket was created but before the gateway recorded success.
    """

    def __init__(self, base_url: str, token: str):
        self._client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )

    def create(
        self, *, tenant_id: str, requester_id: str, summary: str, category: str, conversation_ref: str,
        idempotency_key: str,
    ) -> str:
        resp = self._client.post(
            "/internal/v1/tickets",
            json={
                "tenant_id": tenant_id,
                "requester_id": requester_id,
                "conversation_id": conversation_ref,
                "summary": summary,
                "category": category,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        resp.raise_for_status()
        return resp.json()["ticket"]["id"]


@lru_cache
def get_ticket_provider() -> TicketProvider:
    settings = get_settings()
    return InternalTicketProvider(settings.ticket_service_url, settings.ticket_service_token)


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ticket_id = get_ticket_provider().create(
        tenant_id=ctx.tenant_id,
        requester_id=ctx.user_id,
        summary=args["summary"],
        category=args["category"],
        conversation_ref=ctx.session_id,
        idempotency_key=ctx.idempotency_key,
    )
    return {"ticket_id": ticket_id}


CREATE_TICKET = ToolSpec(
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
    handler=_handle,
    side_effect=True,
)
