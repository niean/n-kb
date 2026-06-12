from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalFilter:
    tags: dict[str, str] = field(default_factory=dict)
    source_kind: str | None = None
    document_status: str | None = None


@dataclass(frozen=True)
class RetrievalQuery:
    query: str
    filters: RetrievalFilter = field(default_factory=RetrievalFilter)
    top_k: int = 5
    min_score: float | None = None


@dataclass(frozen=True)
class RetrievalResult:
    document_id: str
    chunk_id: str
    score: float
    snippet: str
    source: dict[str, Any]
    tags: dict[str, str]
    metadata: dict[str, Any]
