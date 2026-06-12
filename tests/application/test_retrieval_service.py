from app.application.health_service import HealthService
from app.application.retrieval_service import RetrievalService
from app.domain.retrieval import RetrievalFilter, RetrievalResult


class FakeEmbeddingProvider:
    def __init__(self):
        self.queries = []

    def embed_chunks(self, chunks):
        return []

    def embed_query(self, query):
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class FakeVectorIndex:
    def __init__(self):
        self.searches = []
        self.results = [
            RetrievalResult(
                document_id="doc-1",
                chunk_id="chunk-1",
                score=0.9,
                snippet="matching text",
                source={"kind": "upload", "uri": "notes.md"},
                tags={"topic": "rag"},
                metadata={"ordinal": 0},
            )
        ]

    def replace_document(self, document_id, chunks, vectors, tags, source):
        pass

    def search(self, vector, filters, top_k):
        self.searches.append((vector, filters, top_k))
        return self.results


class HealthyDependency:
    def __init__(self, status):
        self.status = status

    def health(self):
        return {"status": self.status}


def test_search_embeds_query_and_delegates_to_vector_index():
    embedding_provider = FakeEmbeddingProvider()
    vector_index = FakeVectorIndex()
    service = RetrievalService(
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
    filters = RetrievalFilter(tags={"topic": "rag"}, source_kind="upload")

    results = service.search(query="rag service", filters=filters, top_k=3)

    assert embedding_provider.queries == ["rag service"]
    assert vector_index.searches == [([0.1, 0.2, 0.3], filters, 3)]
    assert results == vector_index.results


def test_health_service_returns_process_and_dependency_health():
    service = HealthService(
        sqlite=HealthyDependency("ok"),
        qdrant=HealthyDependency("degraded"),
        ollama=object(),
    )

    assert service.process_health() == {"status": "ok"}
    assert service.dependency_health() == {
        "sqlite": {"status": "ok"},
        "qdrant": {"status": "degraded"},
        "ollama": {"status": "unknown"},
    }
