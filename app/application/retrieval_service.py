from app.domain.ports import EmbeddingProvider, VectorIndex
from app.domain.retrieval import RetrievalFilter, RetrievalResult


class RetrievalService:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index

    def search(
        self,
        query: str,
        filters: RetrievalFilter | None = None,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        vector = self._embedding_provider.embed_query(query)
        return self._vector_index.search(vector, filters or RetrievalFilter(), top_k)
