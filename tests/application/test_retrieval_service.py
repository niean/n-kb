import asyncio
import json
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from opentelemetry import trace

from app.domain.evaluation import canonical_json_bytes, compute_integrity

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.application import retrieval_service as retrieval_module
from app.application.health_service import HealthService
from app.application.retrieval_service import RetrievalService
from app.domain.document import Document, DocumentSource, DocumentStatus
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
                metadata={"ordinal": 0, "vector": [1.0, 2.0]},
            )
        ]

    def replace_document(self, document_id, chunks, vectors, tags, source):
        pass

    def search(self, vector, filters, top_k):
        self.searches.append((vector, filters, top_k))
        return self.results


class FakeDocumentRepository:
    def __init__(self, documents):
        self.documents = documents
        self.batch_calls = 0

    def get_document(self, document_id):
        return self.documents.get(document_id)

    def get_documents_by_ids(self, document_ids):
        self.batch_calls += 1
        wanted = set(document_ids)
        return [doc for doc in self.documents.values() if doc.id in wanted]


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


def _make_document(status):
    return Document(
        id="doc-1",
        title="Notes",
        source=DocumentSource(
            kind="upload", uri="notes.md", display_name="notes.md", metadata={}
        ),
        content_hash="dochash-1",
        content_type="text/plain",
        size_bytes=10,
        status=status,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2),
    )


def test_search_attaches_document_revision_for_indexed_documents():
    repository = FakeDocumentRepository({"doc-1": _make_document(DocumentStatus.INDEXED)})
    service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=FakeVectorIndex(),
        document_repository=repository,
    )

    results = service.search(query="rag service", top_k=3)

    assert results[0].document_revision == "dochash-1"
    assert results[0].chunk_content_hash is None


def test_search_filters_out_missing_or_non_indexed_documents():
    for document in (None, _make_document(DocumentStatus.UPLOADED)):
        repository = FakeDocumentRepository({"doc-1": document} if document else {})
        service = RetrievalService(
            embedding_provider=FakeEmbeddingProvider(),
            vector_index=FakeVectorIndex(),
            document_repository=repository,
        )

        results = service.search(query="rag service", top_k=3)

        assert results == []


def test_search_keeps_only_indexed_results_with_revision():
    vector_index = FakeVectorIndex()
    vector_index.results = [
        RetrievalResult(
            document_id="doc-keep",
            chunk_id="chunk-keep",
            score=0.9,
            snippet="matching text",
            source={"kind": "upload", "uri": "notes.md"},
            tags={"topic": "rag"},
            metadata={"ordinal": 0},
        ),
        RetrievalResult(
            document_id="doc-missing",
            chunk_id="chunk-missing",
            score=0.8,
            snippet="stale hit",
            source={"kind": "upload", "uri": "gone.md"},
            tags={},
            metadata={},
        ),
        RetrievalResult(
            document_id="doc-uploaded",
            chunk_id="chunk-uploaded",
            score=0.7,
            snippet="not indexed yet",
            source={"kind": "upload", "uri": "draft.md"},
            tags={},
            metadata={},
        ),
    ]
    repository = FakeDocumentRepository(
        {
            "doc-keep": replace(_make_document(DocumentStatus.INDEXED), id="doc-keep"),
            "doc-uploaded": replace(_make_document(DocumentStatus.UPLOADED), id="doc-uploaded"),
        }
    )
    service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
        document_repository=repository,
    )

    results = service.search(query="rag service", top_k=3)

    assert [result.document_id for result in results] == ["doc-keep"]
    assert results[0].document_revision == "dochash-1"


def test_search_fetches_documents_in_single_batched_call():
    """回填去重后一次批量查询仓储，替代逐结果 N 次 SELECT。"""
    def make(index):
        document = _make_document(DocumentStatus.INDEXED)
        return replace(document, id=f"doc-{index}")

    repository = FakeDocumentRepository({f"doc-{i}": make(i) for i in range(1, 4)})
    vector_index = FakeVectorIndex()
    vector_index.results = [
        RetrievalResult(
            document_id=f"doc-{i}",
            chunk_id=f"chunk-{i}",
            score=0.9 - i * 0.1,
            snippet="matching text",
            source={"kind": "upload", "uri": "notes.md"},
            tags={},
            metadata={},
        )
        for i in (1, 2, 3)
    ]
    vector_index.results[2] = replace(vector_index.results[2], document_id="doc-1")
    service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
        document_repository=repository,
    )

    results = service.search(query="rag service", top_k=3)

    assert repository.batch_calls == 1
    assert [result.document_id for result in results] == ["doc-1", "doc-2", "doc-1"]


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


def _make_service(embedding_provider=None, vector_index=None, **kwargs):
    return RetrievalService(
        embedding_provider=embedding_provider or FakeEmbeddingProvider(),
        vector_index=vector_index or FakeVectorIndex(),
        **kwargs,
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


# ---------------------------------------------------------------------------
# T4: search_with_context 检索上下文
# ---------------------------------------------------------------------------


class FailingVectorIndex(FakeVectorIndex):
    def search(self, vector, filters, top_k):
        raise RuntimeError("qdrant down")


class FakeCorpusRevisionService:
    def __init__(self, revision):
        self.revision = revision
        self.calls = 0

    def current(self):
        self.calls += 1
        return self.revision


class RecordingCaptureHook:
    def __init__(self):
        self.begin_attempts = 0
        self.successes = []
        self.failures = []
        self.raise_on_success = False
        self.raise_on_failure = False

    def begin_attempt(self):
        self.begin_attempts += 1

    def capture_success(self, context, query, filters, top_k, results):
        if self.raise_on_success:
            raise RuntimeError("capture hook exploded")
        self.successes.append((context, query, filters, top_k, results))

    def capture_failure(self, context, query, filters, top_k, exc):
        if self.raise_on_failure:
            raise RuntimeError("capture hook exploded")
        self.failures.append((context, query, filters, top_k, exc))


def _response_projection(results):
    return retrieval_module._response_projection(results)


def test_search_with_context_returns_outcome_with_results_and_context():
    vector_index = FakeVectorIndex()
    service = _make_service(vector_index=vector_index)

    outcome = service.search_with_context(query="rag service", top_k=3)

    assert isinstance(outcome, retrieval_module.RetrievalOutcome)
    assert outcome.results == vector_index.results
    context = outcome.context
    assert isinstance(context, retrieval_module.RetrievalContext)
    assert context.retrieval_id
    assert context.corpus_revision is None
    expected_bytes = canonical_json_bytes(_response_projection(vector_index.results))
    assert context.integrity == compute_integrity(
        expected_bytes, expected_bytes, truncated=False, incomplete=False
    )


def test_retrieval_id_is_unique_across_searches():
    service = _make_service()

    first = service.search_with_context(query="query-a").context.retrieval_id
    second = service.search_with_context(query="query-b").context.retrieval_id

    assert first != second


def test_captured_at_carries_explicit_utc_offset():
    service = _make_service()

    context = service.search_with_context(query="rag service").context

    parsed = datetime.fromisoformat(context.captured_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_trace_ref_matches_recorded_retrieval_span(span_exporter):
    span_exporter.clear()
    service = _make_service()

    outcome = service.search_with_context(query="rag service", top_k=3)

    span = _last_retrieval_span(span_exporter)
    trace_ref = outcome.context.trace_ref
    assert isinstance(trace_ref, dict)
    assert re.fullmatch(r"[0-9a-f]{32}", trace_ref["trace_id"])
    assert trace_ref["trace_id"] == format(span.context.trace_id, "032x")
    assert re.fullmatch(r"[0-9a-f]{16}", trace_ref["span_id"])
    assert trace_ref["span_id"] == format(span.context.span_id, "016x")
    assert trace_ref["sampled"] is bool(span.context.trace_flags.sampled)


def test_trace_ref_is_none_without_valid_span(span_exporter, monkeypatch):
    span_exporter.clear()

    def broken(*args, **kwargs):
        raise RuntimeError("tracer unavailable")

    monkeypatch.setattr(ts.trace, "get_tracer", broken)
    vector_index = FakeVectorIndex()
    service = _make_service(vector_index=vector_index)

    outcome = service.search_with_context(query="rag service", top_k=3)

    assert outcome.context.trace_ref is None
    assert outcome.results == vector_index.results


def test_corpus_revision_taken_from_revision_service():
    revision_service = FakeCorpusRevisionService("rev-fixed")
    service = _make_service(corpus_revision=revision_service)

    context = service.search_with_context(query="rag service").context

    assert revision_service.calls == 1
    assert context.corpus_revision == "rev-fixed"


def test_corpus_revision_none_when_revision_service_reports_none():
    service = _make_service(corpus_revision=FakeCorpusRevisionService(None))

    context = service.search_with_context(query="rag service").context

    assert context.corpus_revision is None


def test_search_failure_keeps_context_and_reraises_original_error():
    hook = RecordingCaptureHook()
    service = _make_service(vector_index=FailingVectorIndex(), capture_hook=hook)

    with pytest.raises(RuntimeError, match="qdrant down"):
        service.search_with_context(query="rag service", top_k=3)

    assert hook.begin_attempts == 1
    assert hook.successes == []
    assert len(hook.failures) == 1
    context, query, filters, top_k, exc = hook.failures[0]
    assert isinstance(context, retrieval_module.RetrievalContext)
    assert context.retrieval_id
    # 失败路径保留不完整占位 integrity，消费方不得当作响应证据读取。
    assert context.integrity["truncated"] is False
    assert context.integrity["incomplete"] is True
    assert context.integrity["observed_bytes"] == 0
    assert context.integrity["original_bytes"] == 0
    assert query == "rag service"
    assert top_k == 3
    assert isinstance(exc, RuntimeError)


def test_response_integrity_marks_incomplete_when_projection_unserializable():
    class NaNScoreVectorIndex(FakeVectorIndex):
        def __init__(self):
            super().__init__()
            self.results = [
                RetrievalResult(
                    document_id="doc-nan",
                    chunk_id="chunk-nan",
                    score=float("nan"),
                    snippet="matching text",
                    source={"kind": "upload", "uri": "notes.md"},
                    tags={},
                    metadata={},
                )
            ]

    service = _make_service(vector_index=NaNScoreVectorIndex())

    outcome = service.search_with_context(query="rag service", top_k=3)

    assert outcome.results[0].score != outcome.results[0].score  # NaN 透传
    assert outcome.context.integrity["truncated"] is False
    assert outcome.context.integrity["incomplete"] is True
    assert outcome.context.integrity["observed_bytes"] == 0
    assert outcome.context.integrity["original_bytes"] == 0


def test_capture_hook_fault_does_not_change_result_or_error():
    success_hook = RecordingCaptureHook()
    success_hook.raise_on_success = True
    vector_index = FakeVectorIndex()
    service = _make_service(vector_index=vector_index, capture_hook=success_hook)

    outcome = service.search_with_context(query="rag service", top_k=3)
    assert outcome.results == vector_index.results

    failure_hook = RecordingCaptureHook()
    failure_hook.raise_on_failure = True
    service = _make_service(vector_index=FailingVectorIndex(), capture_hook=failure_hook)
    with pytest.raises(RuntimeError, match="qdrant down"):
        service.search_with_context(query="rag service", top_k=3)


def test_concurrent_searches_keep_contexts_isolated():
    barrier = threading.Barrier(2)

    class QueryEmbeddingProvider(FakeEmbeddingProvider):
        def embed_query(self, query):
            self.queries.append(query)
            return query

    class BarrierVectorIndex(FakeVectorIndex):
        def search(self, vector, filters, top_k):
            barrier.wait(timeout=5)
            return [
                RetrievalResult(
                    document_id=f"doc-{vector}",
                    chunk_id=f"chunk-{vector}",
                    score=1.0,
                    snippet="matching text",
                    source={"kind": "upload", "uri": "notes.md"},
                    tags={},
                    metadata={},
                )
            ]

    hook = RecordingCaptureHook()
    service = _make_service(
        embedding_provider=QueryEmbeddingProvider(),
        vector_index=BarrierVectorIndex(),
        capture_hook=hook,
    )

    async def run(query):
        return await asyncio.to_thread(service.search_with_context, query)

    async def main():
        return await asyncio.gather(run("query-a"), run("query-b"))

    first, second = asyncio.run(main())

    assert first.results[0].document_id == "doc-query-a"
    assert second.results[0].document_id == "doc-query-b"
    assert first.context.retrieval_id != second.context.retrieval_id
    assert {(query, context.retrieval_id) for context, query, *_ in hook.successes} == {
        ("query-a", first.context.retrieval_id),
        ("query-b", second.context.retrieval_id),
    }
