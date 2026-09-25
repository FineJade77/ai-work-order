from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import get_settings
from app.tools.base import ToolContext, ToolSpec

# Categories are not a fixed list: each tenant's knowledge base defines its own (IT, HR, admin...),
# and the agent passes the category of the entry it answered from. Bounded like ticket-service's column.
CATEGORY_MAX_CHARS = 50


class TicketProvider(ABC):
    @abstractmethod
    async def create(
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
        self._client = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )

    async def create(
        self, *, tenant_id: str, requester_id: str, summary: str, category: str, conversation_ref: str,
        idempotency_key: str,
    ) -> str:
        resp = await self._client.post(
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


async def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ticket_id = await get_ticket_provider().create(
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
    description="知识库方案无法解决员工问题，或员工要求转人工时，为其创建一张工单，交给相关同事处理。",
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "问题描述，包含已尝试过的知识库方案",
            },
            "category": {
                "type": "string",
                "minLength": 1,
                "maxLength": CATEGORY_MAX_CHARS,
                "description": "问题类别：取自命中的知识条目的类别，无法判断时填 other",
            },
        },
        "required": ["summary", "category"],
        "additionalProperties": False,
    },
    handler=_handle,
    side_effect=True,
)
