import json
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from app.domain.matching import Hit, KnowledgeItem, rank

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "knowledge.json"


class KnowledgeRepository(ABC):
    """The seam for replacing V1's static data with a database or vector search later.
    The HTTP contract of /knowledge/search stays the same either way."""

    @abstractmethod
    def search(self, tenant_id: str, query: str, limit: int) -> list[Hit]: ...


class StaticKnowledgeRepository(KnowledgeRepository):
    """Hardcoded per-tenant data loaded once at startup; changing it means a redeploy."""

    def __init__(self, path: Path):
        tenants = json.loads(path.read_text(encoding="utf-8"))["tenants"]
        self._items: dict[str, list[KnowledgeItem]] = {
            tenant_id: [KnowledgeItem(**item) for item in tenant["items"]] for tenant_id, tenant in tenants.items()
        }

    def search(self, tenant_id: str, query: str, limit: int) -> list[Hit]:
        # Unknown tenant -> no results. The tenant registry lives in app-service, not here.
        return rank(query, self._items.get(tenant_id, []), limit)


@lru_cache
def get_repository() -> KnowledgeRepository:
    return StaticKnowledgeRepository(DATA_FILE)
