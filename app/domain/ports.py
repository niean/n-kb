from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector
from app.domain.retrieval import RetrievalFilter, RetrievalResult


@dataclass(frozen=True)
class StoredObject:
    path: Path
    size_bytes: int
    content_hash: str
    content_type: str


class ObjectStore(Protocol):
    def save(self, document_id: str, filename: str, content: bytes) -> StoredObject: ...

    def read_text(self, document_id: str) -> str: ...

    def delete(self, document_id: str) -> None: ...


class DocumentParser(Protocol):
    def parse(self, filename: str, content: bytes) -> str: ...


class TextSplitter(Protocol):
    def split(self, document_id: str, text: str) -> list[Chunk]: ...


class EmbeddingProvider(Protocol):
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddingVector]: ...

    def embed_query(self, query: str) -> list[float]: ...


class VectorIndex(Protocol):
    def replace_document(
        self,
        document_id: str,
        chunks: list[Chunk],
        vectors: list[EmbeddingVector],
        tags: dict[str, str],
        source: dict[str, str],
    ) -> None: ...

    def search(
        self,
        vector: list[float],
        filters: RetrievalFilter,
        top_k: int,
    ) -> list[RetrievalResult]: ...

    def health(self) -> dict[str, object]: ...


class DependencyHealth(Protocol):
    def health(self) -> dict[str, object]: ...
