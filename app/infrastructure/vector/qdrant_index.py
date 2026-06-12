from dataclasses import dataclass
from enum import StrEnum
from typing import Any
import uuid

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
except ModuleNotFoundError:
    QdrantClient = None

    class _Distance(StrEnum):
        COSINE = "Cosine"

    @dataclass(frozen=True)
    class _VectorParams:
        size: int
        distance: _Distance

    @dataclass(frozen=True)
    class _MatchValue:
        value: Any

    @dataclass(frozen=True)
    class _FieldCondition:
        key: str
        match: _MatchValue

    @dataclass(frozen=True)
    class _HasIdCondition:
        has_id: list[str]

    @dataclass(frozen=True)
    class _Filter:
        must: list[Any] | None = None
        must_not: list[Any] | None = None

    @dataclass(frozen=True)
    class _FilterSelector:
        filter: _Filter

    @dataclass(frozen=True)
    class _PointStruct:
        id: str
        vector: list[float]
        payload: dict[str, Any]

    class models:
        Distance = _Distance
        VectorParams = _VectorParams
        MatchValue = _MatchValue
        FieldCondition = _FieldCondition
        HasIdCondition = _HasIdCondition
        Filter = _Filter
        FilterSelector = _FilterSelector
        PointStruct = _PointStruct

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector
from app.domain.retrieval import RetrievalFilter, RetrievalResult


def build_point(
    chunk: Chunk,
    vector: EmbeddingVector,
    tags: dict[str, str],
    source: dict[str, str],
    document_status: str = "indexed",
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.id)),
        "vector": vector.values,
        "payload": {
            "document_id": chunk.document_id,
            "chunk_id": chunk.id,
            "ordinal": chunk.ordinal,
            "text": chunk.text,
            "content_hash": chunk.content_hash,
            "document_status": document_status,
            "tags": dict(tags),
            "source_kind": source.get("kind"),
            "source_uri": source.get("uri"),
            "metadata": dict(chunk.metadata),
        },
    }


class QdrantVectorIndex:
    def __init__(self, qdrant_url: str, collection_name: str, client: Any | None = None):
        self.qdrant_url = qdrant_url
        self.collection_name = collection_name
        if client is None and QdrantClient is None:
            raise RuntimeError("qdrant_client_not_installed")
        self.client = client or QdrantClient(url=qdrant_url)

    def _collection_names(self) -> set[str]:
        collections = self.client.get_collections().collections
        return {collection.name for collection in collections}

    def _ensure_collection(self, dimensions: int) -> None:
        if self.collection_name in self._collection_names():
            return
        vectors_config = models.VectorParams(size=dimensions, distance=models.Distance.COSINE)
        if hasattr(self.client, "create_collection"):
            try:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=vectors_config,
                )
                return
            except Exception:
                # Older/fake clients may only expose recreate_collection.
                pass
        self.client.recreate_collection(
            collection_name=self.collection_name,
            vectors_config=vectors_config,
        )

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))

    @staticmethod
    def _coerce_point(point: dict[str, Any]) -> Any:
        return models.PointStruct(id=point["id"], vector=point["vector"], payload=point["payload"])

    def _delete_stale_points(self, document_id: str, current_chunk_ids: set[str]) -> None:
        must = [models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
        if current_chunk_ids:
            must_not = [
                models.HasIdCondition(has_id=[self._point_id(chunk_id) for chunk_id in current_chunk_ids])
            ]
        else:
            must_not = []
        point_filter = models.Filter(must=must, must_not=must_not)
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=point_filter),
            )
        except TypeError:
            self.client.delete(collection_name=self.collection_name, points_selector=point_filter)

    def replace_document(
        self,
        document_id: str,
        chunks: list[Chunk],
        vectors: list[EmbeddingVector],
        tags: dict[str, str],
        source: dict[str, str],
    ) -> None:
        if len(chunks) != len(vectors):
            raise RuntimeError("embedding_vector_mismatch")
        if not chunks:
            self._delete_stale_points(document_id, set())
            return
        if any(chunk.document_id != document_id for chunk in chunks):
            raise RuntimeError("embedding_vector_mismatch")
        vector_by_chunk_id = {vector.chunk_id: vector for vector in vectors}
        if set(vector_by_chunk_id) != {chunk.id for chunk in chunks}:
            raise RuntimeError("embedding_vector_mismatch")
        first_vector = vectors[0]
        self._ensure_collection(first_vector.dimensions)
        point_dicts = [build_point(chunk, vector_by_chunk_id[chunk.id], tags, source) for chunk in chunks]
        points = point_dicts
        try:
            self.client.upsert(collection_name=self.collection_name, points=points)
        except Exception:
            points = [self._coerce_point(point) for point in point_dicts]
            self.client.upsert(collection_name=self.collection_name, points=points)
        self._delete_stale_points(document_id, {chunk.id for chunk in chunks})

    def _build_filter(self, filters: RetrievalFilter) -> Any | None:
        conditions: list[Any] = []
        for key, value in filters.tags.items():
            conditions.append(models.FieldCondition(key=f"tags.{key}", match=models.MatchValue(value=value)))
        if filters.source_kind is not None:
            conditions.append(models.FieldCondition(key="source_kind", match=models.MatchValue(value=filters.source_kind)))
        if filters.document_status is not None:
            conditions.append(
                models.FieldCondition(key="document_status", match=models.MatchValue(value=filters.document_status))
            )
        return models.Filter(must=conditions) if conditions else None

    @staticmethod
    def _payload(hit: Any) -> dict[str, Any]:
        payload = getattr(hit, "payload", None)
        if payload is None and isinstance(hit, dict):
            payload = hit.get("payload")
        return payload or {}

    @staticmethod
    def _score(hit: Any) -> float:
        if isinstance(hit, dict):
            return float(hit.get("score", 0.0))
        return float(getattr(hit, "score", 0.0))

    @staticmethod
    def _snippet(text: str) -> str:
        return text[:500]

    def search(self, vector: list[float], filters: RetrievalFilter, top_k: int) -> list[RetrievalResult]:
        query_filter = self._build_filter(filters)
        try:
            hits = self.client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                query_filter=query_filter,
                limit=top_k,
                with_vectors=False,
                score_threshold=filters.min_score,
            )
        except AttributeError:
            hits = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                query_filter=query_filter,
                limit=top_k,
                with_vectors=False,
                score_threshold=filters.min_score,
            ).points
        results: list[RetrievalResult] = []
        for hit in hits:
            payload = self._payload(hit)
            results.append(
                RetrievalResult(
                    document_id=payload.get("document_id", ""),
                    chunk_id=payload.get("chunk_id", ""),
                    score=self._score(hit),
                    snippet=self._snippet(str(payload.get("text", ""))),
                    source={
                        "kind": payload.get("source_kind"),
                        "uri": payload.get("source_uri"),
                    },
                    tags=dict(payload.get("tags") or {}),
                    metadata=dict(payload.get("metadata") or {"ordinal": payload.get("ordinal")}),
                )
            )
        return results

    def health(self) -> dict[str, object]:
        try:
            self.client.get_collections()
        except Exception:
            return {"status": "error"}
        return {"status": "ok"}
