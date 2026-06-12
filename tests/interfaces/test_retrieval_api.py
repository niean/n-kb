from dataclasses import asdict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.retrieval import RetrievalResult
from app.interfaces.http.routes import register_routes


class FakeRetrievalService:
    def __init__(self):
        self.call = None

    def search(self, query, filters=None, top_k=5):
        self.call = {"query": query, "filters": filters, "top_k": top_k}
        return [
            RetrievalResult(
                document_id="doc-1",
                chunk_id="chunk-1",
                score=0.91,
                snippet="safe snippet",
                source={"kind": "upload", "uri": "note.md"},
                tags={"topic": "rag"},
                metadata={"ordinal": 0, "vector": [1.0, 2.0]},
            )
        ]


def build_client(service=None) -> TestClient:
    app = FastAPI()
    app.state.services = {"retrieval": service or FakeRetrievalService()}
    register_routes(app)
    return TestClient(app)


def test_search_returns_query_and_results_without_vector_fields():
    service = FakeRetrievalService()
    client = build_client(service)

    response = client.post(
        "/retrieval/search",
        json={
            "query": "what is rag",
            "top_k": 3,
            "filters": {"tags": {"topic": "rag"}, "source_kind": "upload", "document_status": "indexed"},
            "min_score": 0.5,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "what is rag",
        "results": [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "score": 0.91,
                "snippet": "safe snippet",
                "source": {"kind": "upload", "uri": "note.md"},
                "tags": {"topic": "rag"},
                "metadata": {"ordinal": 0},
            }
        ],
    }
    assert service.call["query"] == "what is rag"
    assert service.call["top_k"] == 3
    assert asdict(service.call["filters"]) == {
        "tags": {"topic": "rag"},
        "source_kind": "upload",
        "document_status": "indexed",
    }


def test_search_runtime_error_returns_infrastructure_error_without_details():
    class FailingRetrievalService(FakeRetrievalService):
        def search(self, query, filters=None, top_k=5):
            raise RuntimeError("qdrant host secret")

    client = build_client(FailingRetrievalService())

    response = client.post("/retrieval/search", json={"query": "rag"})

    assert response.status_code == 502
    assert response.json() == {"error": {"code": "infrastructure_error", "message": "infrastructure_error"}}



def test_search_rejects_top_k_above_limit():
    client = build_client()

    response = client.post("/retrieval/search", json={"query": "rag", "top_k": 1000})

    assert response.status_code == 422
    assert "detail" in response.json()



def test_search_rejects_blank_query():
    client = build_client()

    response = client.post("/retrieval/search", json={"query": "", "top_k": 5})

    assert response.status_code == 422
    assert "detail" in response.json()
