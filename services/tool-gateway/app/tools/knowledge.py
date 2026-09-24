from typing import Any

from shared.db.models import KnowledgeItem
from shared.db.session import app_session_factory, tenant_scope
from sqlalchemy import func, or_

from app.tools.base import ToolContext, ToolSpec

RESULT_LIMIT = 5


def search_knowledge(query: str, tenant_id: str) -> list[dict[str, Any]]:
    """v1 search: ILIKE substring match, ranked by pg_trgm similarity. No vectorization yet."""
    Session = app_session_factory()
    with Session() as session, tenant_scope(session, tenant_id):
        pattern = f"%{query}%"
        score = func.greatest(
            func.similarity(KnowledgeItem.question, query),
            func.similarity(KnowledgeItem.answer, query),
        )
        rows = (
            session.query(KnowledgeItem, score.label("score"))
            .filter(or_(KnowledgeItem.question.ilike(pattern), KnowledgeItem.answer.ilike(pattern)))
            .order_by(score.desc())
            .limit(RESULT_LIMIT)
            .all()
        )
        return [
            {"question": item.question, "answer": item.answer, "score": round(float(item_score), 4)}
            for item, item_score in rows
        ]


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"results": search_knowledge(query=args["query"], tenant_id=ctx.tenant_id)}


SEARCH_KNOWLEDGE = ToolSpec(
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
    handler=_handle,
    side_effect=False,
)
