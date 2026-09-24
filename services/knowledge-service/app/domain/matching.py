"""V1 keyword matching over a small in-memory item list. No tokenizer, no vectors.

Character bigrams work for Chinese without word segmentation: "证书过期" shares the
bigrams 证书/过期 with "证书可能已过期" even though they're different phrases.
"""

import re
from dataclasses import dataclass

MIN_SCORE = 0.3
ANSWER_WEIGHT = 0.6  # a hit in the question says more about relevance than one in the answer

_NOISE = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    category: str
    question: str
    answer: str


@dataclass(frozen=True)
class Hit:
    item: KnowledgeItem
    score: float


def normalize(text: str) -> str:
    return _NOISE.sub("", text).lower()


def bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def score(query: str, item: KnowledgeItem) -> float:
    """Share of the query's bigrams found in the item, 1.0 when the whole query appears in the question."""
    q = normalize(query)
    if not q:
        return 0.0
    question = normalize(item.question)
    if q in question:
        return 1.0
    q_grams = bigrams(q)
    in_question = len(q_grams & bigrams(question)) / len(q_grams)
    in_answer = len(q_grams & bigrams(normalize(item.answer))) / len(q_grams)
    return max(in_question, ANSWER_WEIGHT * in_answer)


def rank(query: str, items: list[KnowledgeItem], limit: int) -> list[Hit]:
    hits = [Hit(item, round(score(query, item), 4)) for item in items]
    hits = [h for h in hits if h.score >= MIN_SCORE]
    hits.sort(key=lambda h: (-h.score, h.item.id))
    return hits[:limit]
