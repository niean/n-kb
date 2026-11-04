from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from app.application import telemetry_semantics as ts
from app.application.retrieval_service import RetrievalService
from app.domain.retrieval import RetrievalResult
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider
from app.infrastructure.observability.instrumentors import instrument_app
from app.infrastructure.observability.logging import HTTP_COMPLETED_EVENT
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
        "min_score": 0.5,
    }

def test_search_uses_default_min_score_when_not_provided():
    service = FakeRetrievalService()
    client = build_client(service)

    response = client.post("/retrieval/search", json={"query": "what is rag"})

    assert response.status_code == 200
    assert service.call["filters"].min_score == 0.5


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


# ---------------------------------------------------------------------------
# T8: 检索链路 API 集成 span 树（SERVER -> nkb.retrieval.search -> embed -> HTTPX）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def otel_capture():
    """T2/T7 既定模式：附加到现有全局 SDK provider，或设置自有 provider。"""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = trace.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(processor)
        trace.set_tracer_provider(owning)
        provider = owning
    else:
        try:
            provider.add_span_processor(processor)
        except Exception:
            pass
    return SimpleNamespace(provider=provider, exporter=exporter)


class _HttpVectorIndex:
    """真实 httpx 调用 Qdrant REST 的假向量索引（不落 SDK）。"""

    def __init__(self, tracer_provider):
        self.client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"result": [{"score": 0.9}]}
                )
            )
        )
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor.instrument_client(
            self.client, tracer_provider=tracer_provider
        )

    def replace_document(self, document_id, chunks, vectors, tags, source):
        raise RuntimeError("not used in retrieval tests")

    def search(self, vector, filters, top_k):
        response = self.client.post(
            "http://qdrant.test/collections/n-kb/points/search", json={"limit": top_k}
        )
        response.raise_for_status()
        return [
            RetrievalResult(
                document_id="doc-1",
                chunk_id="chunk-1",
                score=0.9,
                snippet="safe snippet",
                source={"kind": "upload", "uri": "note.md"},
                tags={"topic": "rag"},
                metadata={"ordinal": 0},
            )
        ]


def _build_traced_retrieval_app(otel_capture):
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    embed_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 3},
            )
        )
    )
    HTTPXClientInstrumentor.instrument_client(
        embed_client, tracer_provider=otel_capture.provider
    )
    embedding_provider = OllamaEmbeddingProvider(
        base_url="http://ollama.test",
        model="bge-m3",
        client=embed_client,
        embedding_observer=ts.embedding_observer,
    )
    service = RetrievalService(
        embedding_provider=embedding_provider,
        vector_index=_HttpVectorIndex(otel_capture.provider),
    )
    app = FastAPI()
    app.state.services = {"retrieval": service}
    register_routes(app)
    assert instrument_app(
        app, settings=SimpleNamespace(mcp_path="/mcp"), tracer_provider=otel_capture.provider
    )
    return TestClient(app)


def _children(spans, parent_span_id):
    return [s for s in spans if s.parent is not None and s.parent.span_id == parent_span_id]


def test_search_api_emits_server_retrieval_embed_httpx_span_tree(otel_capture):
    otel_capture.exporter.clear()
    client = _build_traced_retrieval_app(otel_capture)

    response = client.post(
        "/retrieval/search",
        json={"query": "what is rag", "top_k": 3, "filters": {"tags": {"topic": "rag"}}},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["snippet"] == "safe snippet"

    spans = otel_capture.exporter.get_finished_spans()
    servers = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(servers) == 1
    server = servers[0]
    assert server.name == "POST /retrieval/search"

    retrievals = [s for s in spans if s.name == ts.RETRIEVAL_SPAN_NAME]
    assert len(retrievals) == 1
    retrieval = retrievals[0]
    assert retrieval.kind == SpanKind.INTERNAL
    assert retrieval.context.trace_id == server.context.trace_id
    assert retrieval.parent.span_id == server.context.span_id
    assert retrieval.attributes[ts.NKB_RESULT_COUNT] == 1
    assert retrieval.attributes["nkb.retrieval.filter_tags"] == 1
    assert retrieval.attributes["nkb.retrieval.outcome"] == ts.OUTCOME_SUCCESS

    embeddings = [
        s
        for s in spans
        if s.kind == SpanKind.CLIENT and s.name == "embeddings bge-m3"
    ]
    assert len(embeddings) == 1
    embeddings_span = embeddings[0]
    assert embeddings_span.parent.span_id == retrieval.context.span_id
    assert embeddings_span.attributes[ts.GEN_AI_OPERATION_NAME] == "embeddings"
    assert embeddings_span.attributes[ts.GEN_AI_REQUEST_MODEL] == "bge-m3"

    httpx_spans = [s for s in spans if s.kind == SpanKind.CLIENT and s.name == "POST"]
    ollama_calls = [
        s for s in httpx_spans if s.attributes.get("http.url") == "http://ollama.test/api/embed"
    ]
    qdrant_calls = [
        s for s in httpx_spans if s.attributes.get("http.url") == "http://qdrant.test/collections/n-kb/points/search"
    ]
    assert len(ollama_calls) == 1
    assert ollama_calls[0].parent.span_id == embeddings_span.context.span_id
    assert len(qdrant_calls) == 1
    assert qdrant_calls[0].parent.span_id == retrieval.context.span_id

    # SERVER 下挂且同 trace；每请求恰好一次入站完成事实事件。
    direct_children = _children(spans, server.context.span_id)
    assert retrieval in direct_children
    assert [e.name for e in server.events].count(HTTP_COMPLETED_EVENT) == 1

    # 同 trace、span id 唯一（同操作共享 Span ID 仅限自身；父子只共享 Trace ID）。
    trace_ids = {s.context.trace_id for s in spans}
    span_ids = [s.context.span_id for s in spans]
    assert trace_ids == {server.context.trace_id}
    assert len(span_ids) == len(set(span_ids))

    # 安全投影：query/filter 值/snippet 不进入任何 span 属性或事件。
    dumped = "".join(_span_dump(span) for span in spans)
    assert "what is rag" not in dumped
    assert "secret-tag" not in dumped
    assert "safe snippet" not in dumped


def _span_dump(span):
    import json

    return json.dumps(
        {
            "name": span.name,
            "attributes": dict(span.attributes or {}),
            "events": [dict(event.attributes or {}) for event in span.events],
        },
        default=str,
    )
