from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.errors import ApiError
from app.core.security import require_service_token
from app.core.tenancy import parse_tenant_id
from app.repositories.knowledge import KnowledgeRepository, get_repository
from app.schemas.knowledge import KnowledgeResult, SearchResponse

router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(require_service_token)])


@router.get("/search")
def search(
    repo: Annotated[KnowledgeRepository, Depends(get_repository)],
    tenant_id: str,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> SearchResponse:
    try:
        canonical_tenant = str(parse_tenant_id(tenant_id))
    except ValueError as exc:
        raise ApiError(400, "INVALID_TENANT", str(exc)) from exc
    hits = repo.search(canonical_tenant, q, limit)
    return SearchResponse(
        results=[
            KnowledgeResult(
                id=h.item.id, category=h.item.category, question=h.item.question, answer=h.item.answer, score=h.score
            )
            for h in hits
        ]
    )
