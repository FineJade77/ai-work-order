from pydantic import BaseModel


class KnowledgeResult(BaseModel):
    id: str
    category: str
    question: str
    answer: str
    score: float


class SearchResponse(BaseModel):
    results: list[KnowledgeResult]
