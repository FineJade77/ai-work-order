from functools import lru_cache
from typing import Any

import httpx

from app.core.config import get_settings
from app.tools.base import ToolContext, ToolSpec

RESULT_LIMIT = 5


@lru_cache
def _client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.knowledge_service_url,
        headers={"Authorization": f"Bearer {settings.knowledge_service_token}"},
        timeout=5.0,
    )


async def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await _client().get(
        "/internal/v1/knowledge/search",
        params={"tenant_id": ctx.tenant_id, "q": args["query"], "limit": RESULT_LIMIT},
    )
    resp.raise_for_status()
    # category is passed on because a ticket filed after this answer is categorised by it.
    return {
        "results": [
            {"category": r["category"], "question": r["question"], "answer": r["answer"], "score": r["score"]}
            for r in resp.json()["results"]
        ]
    }


SEARCH_KNOWLEDGE = ToolSpec(
    name="search_knowledge",
    description="在企业知识库中检索与员工问题相关的问答条目。回答员工的问题前应先调用。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "员工问题的关键词或原句"}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_handle,
    side_effect=False,
)
