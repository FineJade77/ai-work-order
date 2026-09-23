from sqlalchemy import func, or_

from app.models import KnowledgeResult
from shared.db.models import KnowledgeItem
from shared.db.session import app_session_factory, tenant_scope

RESULT_LIMIT = 5


def search_knowledge_impl(query: str, tenant_id: str) -> list[KnowledgeResult]:
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
            KnowledgeResult(question=item.question, answer=item.answer, score=float(item_score))
            for item, item_score in rows
        ]
