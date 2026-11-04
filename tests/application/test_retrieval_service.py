import asyncio
import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.application.health_service import HealthService
from app.application.retrieval_service import RetrievalService
from app.domain.retrieval import RetrievalFilter, RetrievalResult


@pytest.fixture(scope="module")
def span_exporter():
    """T2/T7 既定模式：附加到现有全局 SDK provider，或设置自有 provider。"""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = trace.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(processor)
        trace.set_tracer_provider(owning)
    else:
        try:
            provider.add_span_processor(processor)
        except Exception:
            pass
    return exporter


def _last_retrieval_span(span_exporter):
    spans = [
        span
        for span in span_exporter.get_finished_spans()
        if span.name == ts.RETRIEVAL_SPAN_NAME
    ]
    assert spans, "expected nkb.retrieval.search span"
    return spans[-1]


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


# ---------------------------------------------------------------------------
# T8: nkb.retrieval.search 操作语义
# ---------------------------------------------------------------------------


def _make_service(embedding_provider=None, vector_index=None):
    return RetrievalService(
        embedding_provider=embedding_provider or FakeEmbeddingProvider(),
        vector_index=vector_index or FakeVectorIndex(),
    )


def test_search_wraps_nkb_retrieval_search_span_with_safe_attributes(span_exporter):
    span_exporter.clear()
    embedding_provider = FakeEmbeddingProvider()
    vector_index = FakeVectorIndex()
    service = RetrievalService(
        embedding_provider=embedding_provider, vector_index=vector_index
    )
    filters = RetrievalFilter(tags={"topic": "secret-tag-value"}, source_kind="upload")

    results = service.search(query="find-the-secret-query", filters=filters, top_k=3)

    assert results == vector_index.results
    span = _last_retrieval_span(span_exporter)
    assert span.kind == trace.SpanKind.INTERNAL
    assert span.attributes["nkb.retrieval.result_count"] == 1
    assert span.attributes["nkb.retrieval.filter_tags"] == 1
    assert span.attributes["nkb.retrieval.filter_source_kind"] == 1
    assert span.attributes["nkb.retrieval.outcome"] == "success"
    dumped = json.dumps(span.attributes, default=str)
    assert "find-the-secret-query" not in dumped
    assert "secret-tag-value" not in dumped
    assert "upload" not in dumped


def test_search_records_retrieval_once_while_span_current(span_exporter, monkeypatch):
    span_exporter.clear()
    calls = []
    current_names = []

    def spy(outcome, **kwargs):
        calls.append((outcome, kwargs))
        current = trace.get_current_span()
        current_names.append(current.name if current is not None else None)

    monkeypatch.setattr(ts, "record_retrieval", spy)
    service = _make_service()

    service.search(query="rag service", top_k=3)

    assert len(calls) == 1
    outcome, kwargs = calls[0]
    assert outcome == ts.OUTCOME_SUCCESS
    assert kwargs["result_count"] == 1
    assert "seconds" not in kwargs
    assert current_names == [ts.RETRIEVAL_SPAN_NAME]


def test_search_error_records_once_without_results(span_exporter, monkeypatch):
    span_exporter.clear()

    class FailingVectorIndex(FakeVectorIndex):
        def search(self, vector, filters, top_k):
            raise RuntimeError("qdrant_client_not_installed")

    calls = []

    def spy(outcome, **kwargs):
        calls.append((outcome, kwargs))

    monkeypatch.setattr(ts, "record_retrieval", spy)
    service = _make_service(vector_index=FailingVectorIndex())

    with pytest.raises(RuntimeError, match="qdrant_client_not_installed"):
        service.search(query="rag service", top_k=3)

    assert len(calls) == 1
    outcome, kwargs = calls[0]
    assert outcome == ts.OUTCOME_ERROR
    assert "result_count" not in kwargs or kwargs["result_count"] is None
    span = _last_retrieval_span(span_exporter)
    assert span.attributes["nkb.retrieval.outcome"] == "failure"
    assert span.attributes["error.type"] == "vector_index_failed"
    assert span.status.status_code == trace.StatusCode.ERROR


def test_search_cancelled_propagates_and_records_once(span_exporter, monkeypatch):
    span_exporter.clear()

    class CancellingEmbeddingProvider(FakeEmbeddingProvider):
        def embed_query(self, query):
            raise asyncio.CancelledError()

    calls = []

    def spy(outcome, **kwargs):
        calls.append((outcome, kwargs))

    monkeypatch.setattr(ts, "record_retrieval", spy)
    service = _make_service(embedding_provider=CancellingEmbeddingProvider())

    with pytest.raises(asyncio.CancelledError):
        service.search(query="rag service", top_k=3)

    assert len(calls) == 1
    assert calls[0][0] == ts.OUTCOME_CANCELLED
    span = _last_retrieval_span(span_exporter)
    assert span.attributes["nkb.retrieval.outcome"] == "cancelled"
    assert "error.type" not in span.attributes


def test_search_helper_fault_does_not_change_business_result(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(ts.trace, "get_tracer", broken)
    monkeypatch.setattr(ts, "record_retrieval", broken)
    embedding_provider = FakeEmbeddingProvider()
    vector_index = FakeVectorIndex()
    service = RetrievalService(
        embedding_provider=embedding_provider, vector_index=vector_index
    )
    filters = RetrievalFilter(tags={"topic": "rag"})

    results = service.search(query="rag service", filters=filters, top_k=3)

    assert embedding_provider.queries == ["rag service"]
    assert vector_index.searches[0][2] == 3
    assert results == vector_index.results
