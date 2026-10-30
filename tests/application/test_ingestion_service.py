import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.application.ingestion_service import IngestionService
from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.embedding import EmbeddingVector
from app.domain.indexing import IndexJobStatus, IndexStage
from app.domain.tag import Tag


class FakeDocumentRepository:
    def __init__(self, document=None, content=None, tags=None):
        self.document = document
        self.content = content
        self.tags = tags or []
        self.status_updates = []

    def get_document(self, document_id):
        return self.document if self.document and self.document.id == document_id else None

    def find_by_content_hash(self, content_hash):
        return self.document if self.document and self.document.content_hash == content_hash else None

    def get_content(self, document_id):
        return self.content if self.content and self.content.document_id == document_id else None

    def get_tags(self, document_id):
        return self.tags

    def update_status(self, document_id, status):
        self.status_updates.append((document_id, status))
        if self.document and self.document.id == document_id:
            self.document = Document(
                id=self.document.id,
                title=self.document.title,
                source=self.document.source,
                content_hash=self.document.content_hash,
                content_type=self.document.content_type,
                size_bytes=self.document.size_bytes,
                status=status,
                created_at=self.document.created_at,
                updated_at=self.document.updated_at,
            )

    def save_document(self, document, content, tags):
        raise NotImplementedError

    def list_documents(self, tags=None, status=None):
        raise NotImplementedError


class FakeChunkRepository:
    def __init__(self, replace_error=None, chunks_by_document=None):
        self.replaced = []
        self.replace_error = replace_error
        self.chunks_by_document = {
            document_id: list(chunks)
            for document_id, chunks in (chunks_by_document or {}).items()
        }

    def replace_chunks(self, document_id, chunks):
        if self.replace_error is not None:
            raise self.replace_error
        stored_chunks = list(chunks)
        self.replaced.append((document_id, stored_chunks))
        self.chunks_by_document[document_id] = stored_chunks

    def list_chunks(self, document_id):
        return list(self.chunks_by_document.get(document_id, []))


class FakeIndexJobRepository:
    def __init__(self):
        self.created_jobs = []
        self.updates = []

    def create_job(self, job):
        self.created_jobs.append(job)

    def get_job(self, job_id):
        return next((job for job in self.created_jobs if job.id == job_id), None)

    def update_job(self, job_id, status, stage, error=None):
        self.updates.append((job_id, status, stage, error))
        self.created_jobs = [
            replace(job, status=status, stage=stage, error=error) if job.id == job_id else job
            for job in self.created_jobs
        ]


class FakeParser:
    def __init__(self):
        self.calls = []

    def parse(self, filename, content):
        self.calls.append((filename, content))
        return content.decode("utf-8")


class FakeSplitter:
    def __init__(self):
        self.calls = []

    def split(self, document_id, text):
        self.calls.append((document_id, text))
        return [
            Chunk(
                id=f"{document_id}-chunk-1",
                document_id=document_id,
                ordinal=0,
                text=text[:5],
                content_hash="chunk-hash-1",
                token_count=1,
                metadata={"ordinal": 0},
            ),
            Chunk(
                id=f"{document_id}-chunk-2",
                document_id=document_id,
                ordinal=1,
                text=text[5:],
                content_hash="chunk-hash-2",
                token_count=1,
                metadata={"ordinal": 1},
            ),
        ]


class FakeEmbeddingProvider:
    def __init__(self, vectors=None):
        self.chunk_calls = []
        self.vectors = vectors

    def embed_chunks(self, chunks):
        self.chunk_calls.append(chunks)
        if self.vectors is not None:
            return self.vectors
        return [
            EmbeddingVector(
                chunk_id=chunk.id,
                model="fake-model",
                dimensions=2,
                values=[float(index), float(index + 1)],
            )
            for index, chunk in enumerate(chunks)
        ]

    def embed_query(self, query):
        return [1.0, 2.0]


class FakeVectorIndex:
    def __init__(self, replace_error=None):
        self.replacements = []
        self.calls = []
        self.replace_error = replace_error

    def replace_document(self, document_id, chunks, vectors, tags, source):
        self.calls.append(("replace_document", document_id, chunks, vectors, tags, source))
        if self.replace_error is not None:
            raise self.replace_error
        self.replacements.append((document_id, chunks, vectors, tags, source))

    def search(self, vector, filters, top_k):
        return []


@pytest.fixture
def document():
    now = datetime.now(timezone.utc)
    return Document(
        id="doc-1",
        title="notes.md",
        source=DocumentSource(kind="upload", uri="notes.md", display_name="Notes", metadata={}),
        content_hash="content-hash",
        content_type="text/markdown",
        size_bytes=11,
        status=DocumentStatus.UPLOADED,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def content():
    return DocumentContent(
        document_id="doc-1",
        text="hello world",
        content_hash="content-hash",
        encoding="utf-8",
        created_at=datetime.now(timezone.utc),
    )


def test_index_document_runs_ingestion_pipeline_and_marks_success(document, content):
    document_repository = FakeDocumentRepository(
        document=document,
        content=content,
        tags=[Tag("topic", "rag"), Tag("project", "n-kb")],
    )
    chunk_repository = FakeChunkRepository()
    job_repository = FakeIndexJobRepository()
    parser = FakeParser()
    splitter = FakeSplitter()
    embedding_provider = FakeEmbeddingProvider()
    vector_index = FakeVectorIndex()
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository,
        index_job_repository=job_repository,
        document_parser=parser,
        text_splitter=splitter,
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )

    job = service.index_document("doc-1")

    assert job.document_id == "doc-1"
    assert job.status == IndexJobStatus.SUCCEEDED
    assert job.stage == IndexStage.COMPLETED
    assert len(job_repository.created_jobs) == 1
    job_id = job_repository.created_jobs[0].id
    assert job_repository.get_job(job_id).status == IndexJobStatus.SUCCEEDED
    assert document_repository.status_updates == [
        ("doc-1", DocumentStatus.INDEXING),
        ("doc-1", DocumentStatus.INDEXED),
    ]
    assert parser.calls == [("notes.md", b"hello world")]
    assert splitter.calls == [("doc-1", "hello world")]
    chunks = chunk_repository.replaced[0][1]
    assert [chunk.id for chunk in chunks] == ["doc-1-chunk-1", "doc-1-chunk-2"]
    assert embedding_provider.chunk_calls == [chunks]
    assert [call[0] for call in vector_index.calls] == ["replace_document"]
    replace_document_id, replace_chunks, vectors, tags, source = vector_index.replacements[0]
    assert replace_document_id == "doc-1"
    assert replace_chunks == chunks
    assert [vector.chunk_id for vector in vectors] == [chunk.id for chunk in chunks]
    assert tags == {"topic": "rag", "project": "n-kb"}
    assert source == {"kind": "upload", "uri": "notes.md"}
    assert job_repository.updates == [
        (job_id, IndexJobStatus.RUNNING, IndexStage.PARSING, None),
        (job_id, IndexJobStatus.RUNNING, IndexStage.SPLITTING, None),
        (job_id, IndexJobStatus.RUNNING, IndexStage.EMBEDDING, None),
        (job_id, IndexJobStatus.RUNNING, IndexStage.WRITING_VECTOR_INDEX, None),
        (job_id, IndexJobStatus.SUCCEEDED, IndexStage.COMPLETED, None),
    ]


def test_index_document_raises_document_not_found_when_content_missing(document):
    service = IngestionService(
        document_repository=FakeDocumentRepository(document=document, content=None),
        chunk_repository=FakeChunkRepository(),
        index_job_repository=FakeIndexJobRepository(),
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=FakeVectorIndex(),
    )

    with pytest.raises(ValueError, match="document_not_found"):
        service.index_document("doc-1")


def test_replace_failure_restores_previous_sqlite_chunks_and_marks_failed(document, content):
    document_repository = FakeDocumentRepository(document=document, content=content)
    previous_chunks = [
        Chunk(
            id="doc-1-old-chunk",
            document_id="doc-1",
            ordinal=0,
            text="old text",
            content_hash="old-hash",
            token_count=2,
            metadata={"version": "old"},
        )
    ]
    chunk_repository = FakeChunkRepository(chunks_by_document={"doc-1": previous_chunks})
    job_repository = FakeIndexJobRepository()
    vector_index = FakeVectorIndex(replace_error=RuntimeError("qdrant_unavailable"))
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository,
        index_job_repository=job_repository,
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
    )

    with pytest.raises(RuntimeError, match="qdrant_unavailable"):
        service.index_document("doc-1")

    assert [call[0] for call in vector_index.calls] == ["replace_document"]
    assert len(chunk_repository.replaced) == 2
    assert [chunk.id for chunk in chunk_repository.replaced[0][1]] == [
        "doc-1-chunk-1",
        "doc-1-chunk-2",
    ]
    assert chunk_repository.replaced[1] == ("doc-1", previous_chunks)
    assert chunk_repository.list_chunks("doc-1") == previous_chunks
    assert document_repository.status_updates[-1] == ("doc-1", DocumentStatus.FAILED)
    failed_job = job_repository.get_job(job_repository.created_jobs[0].id)
    assert failed_job.status == IndexJobStatus.FAILED
    assert failed_job.stage == IndexStage.FAILED
    assert job_repository.updates[-1][1:] == (
        IndexJobStatus.FAILED,
        IndexStage.FAILED,
        "RuntimeError",
    )


def test_chunk_persistence_failure_does_not_replace_vectors_and_marks_failed(document, content):
    document_repository = FakeDocumentRepository(document=document, content=content)
    chunk_repository = FakeChunkRepository(
        replace_error=RuntimeError("secret-token-123 private sqlite details")
    )
    job_repository = FakeIndexJobRepository()
    vector_index = FakeVectorIndex()
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository,
        index_job_repository=job_repository,
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
    )

    with pytest.raises(RuntimeError, match="secret-token-123"):
        service.index_document("doc-1")

    assert vector_index.calls == []
    assert vector_index.replacements == []
    assert chunk_repository.replaced == []
    assert document_repository.status_updates[-1] == ("doc-1", DocumentStatus.FAILED)
    failed_job = job_repository.get_job(job_repository.created_jobs[0].id)
    assert failed_job.status == IndexJobStatus.FAILED
    assert failed_job.stage == IndexStage.FAILED
    stored_error = job_repository.updates[-1][3]
    assert stored_error == "RuntimeError"
    assert "secret-token-123" not in stored_error
    assert "private sqlite details" not in stored_error


def test_vector_count_mismatch_fails_without_vector_or_sqlite_mutation(document, content):
    document_repository = FakeDocumentRepository(document=document, content=content)
    chunk_repository = FakeChunkRepository()
    job_repository = FakeIndexJobRepository()
    vector_index = FakeVectorIndex()
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository,
        index_job_repository=job_repository,
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(vectors=[]),
        vector_index=vector_index,
    )

    with pytest.raises(RuntimeError, match="embedding_vector_mismatch"):
        service.index_document("doc-1")

    assert vector_index.replacements == []
    assert vector_index.calls == []
    assert chunk_repository.replaced == []
    assert document_repository.status_updates[-1] == ("doc-1", DocumentStatus.FAILED)
    assert job_repository.updates[-1][1:] == (
        IndexJobStatus.FAILED,
        IndexStage.FAILED,
        "embedding_vector_mismatch",
    )


def test_vector_chunk_id_mismatch_fails_without_vector_or_sqlite_mutation(document, content):
    document_repository = FakeDocumentRepository(document=document, content=content)
    chunk_repository = FakeChunkRepository()
    job_repository = FakeIndexJobRepository()
    vector_index = FakeVectorIndex()
    vectors = [
        EmbeddingVector(
            chunk_id="doc-1-chunk-1",
            model="fake-model",
            dimensions=2,
            values=[0.0, 1.0],
        ),
        EmbeddingVector(
            chunk_id="other-chunk",
            model="fake-model",
            dimensions=2,
            values=[1.0, 2.0],
        ),
    ]
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository,
        index_job_repository=job_repository,
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(vectors=vectors),
        vector_index=vector_index,
    )

    with pytest.raises(RuntimeError, match="embedding_vector_mismatch"):
        service.index_document("doc-1")

    assert vector_index.replacements == []
    assert vector_index.calls == []
    assert chunk_repository.replaced == []
    assert document_repository.status_updates[-1] == ("doc-1", DocumentStatus.FAILED)
    assert job_repository.updates[-1][1:] == (
        IndexJobStatus.FAILED,
        IndexStage.FAILED,
        "embedding_vector_mismatch",
    )


def test_sensitive_exception_message_is_not_persisted(document, content):
    document_repository = FakeDocumentRepository(document=document, content=content)
    job_repository = FakeIndexJobRepository()
    sensitive_text = "secret-token-123 " * 20 + "\nsecond line with private notes"
    vector_index = FakeVectorIndex(replace_error=RuntimeError(sensitive_text))
    service = IngestionService(
        document_repository=document_repository,
        chunk_repository=FakeChunkRepository(),
        index_job_repository=job_repository,
        document_parser=FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
    )

    with pytest.raises(RuntimeError):
        service.index_document("doc-1")

    stored_error = job_repository.updates[-1][3]
    assert stored_error == "RuntimeError"
    assert "secret-token-123" not in stored_error
    assert "second line with private notes" not in stored_error


# ---------------------------------------------------------------------------
# T8: nkb.index.run 与四阶段操作语义
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def span_exporter():
    """T2/T7 既定模式：附加到现有全局 SDK provider，或设置自有 provider。"""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    from opentelemetry import trace as trace_api

    provider = trace_api.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(processor)
        trace_api.set_tracer_provider(owning)
    else:
        try:
            provider.add_span_processor(processor)
        except Exception:
            pass
    return exporter


def _make_service(
    document=None,
    content=None,
    tags=None,
    chunk_repository=None,
    vector_index=None,
    embedding_provider=None,
    document_repository=None,
    document_parser=None,
):
    document_repository = document_repository or FakeDocumentRepository(
        document=document, content=content, tags=tags or []
    )
    return IngestionService(
        document_repository=document_repository,
        chunk_repository=chunk_repository or FakeChunkRepository(),
        index_job_repository=FakeIndexJobRepository(),
        document_parser=document_parser or FakeParser(),
        text_splitter=FakeSplitter(),
        embedding_provider=embedding_provider or FakeEmbeddingProvider(),
        vector_index=vector_index or FakeVectorIndex(),
    )


INDEX_SPAN_NAMES = {
    "nkb.index.run",
    "nkb.index.parse",
    "nkb.index.split",
    "nkb.index.embed",
    "nkb.index.write",
}


def _index_spans(span_exporter):
    return [
        span
        for span in span_exporter.get_finished_spans()
        if span.name in INDEX_SPAN_NAMES
    ]


def test_index_document_wraps_run_and_four_stage_spans(span_exporter, document, content):
    span_exporter.clear()
    service = _make_service(document=document, content=content)

    job = service.index_document("doc-1")

    assert job.status == IndexJobStatus.SUCCEEDED
    spans = _index_spans(span_exporter)
    names = [span.name for span in spans]
    assert set(names) == INDEX_SPAN_NAMES
    run = next(span for span in spans if span.name == "nkb.index.run")
    stages = [span for span in spans if span.name != "nkb.index.run"]
    assert [span.name for span in stages] == [
        "nkb.index.parse",
        "nkb.index.split",
        "nkb.index.embed",
        "nkb.index.write",
    ]
    for span in spans:
        assert span.context.trace_id == run.context.trace_id
        if span is not run:
            assert span.parent.span_id == run.context.span_id
            assert span.attributes["nkb.outcome"] == "success"
    span_ids = [span.context.span_id for span in spans]
    assert len(span_ids) == len(set(span_ids))
    assert run.attributes["nkb.document_id"] == "doc-1"
    assert run.attributes["nkb.chunk_count"] == 2
    assert run.attributes["nkb.outcome"] == "success"
    dumped = json.dumps(
        [(span.name, span.attributes) for span in spans], default=str
    )
    assert "hello world" not in dumped


def test_index_document_records_index_once_while_run_current(
    span_exporter, document, content, monkeypatch
):
    span_exporter.clear()
    calls = []
    current_names = []

    def spy(outcome, **kwargs):
        calls.append((outcome, kwargs))
        current = trace.get_current_span()
        current_names.append(current.name if current is not None else None)

    monkeypatch.setattr(ts, "record_index", spy)
    service = _make_service(document=document, content=content)

    service.index_document("doc-1")

    assert len(calls) == 1
    outcome, kwargs = calls[0]
    assert outcome == ts.OUTCOME_SUCCESS
    assert kwargs["chunk_count"] == 2
    assert current_names == ["nkb.index.run"]


def test_missing_document_records_error_once_without_stage_spans(
    span_exporter, document
):
    span_exporter.clear()
    service = _make_service(document=document, content=None)

    with pytest.raises(ValueError, match="document_not_found"):
        service.index_document("doc-1")

    spans = _index_spans(span_exporter)
    assert [span.name for span in spans] == ["nkb.index.run"]
    run = spans[0]
    assert run.attributes["nkb.outcome"] == "error"
    assert run.attributes["error.type"] == "other"
    assert "nkb.chunk_count" not in run.attributes


def test_parse_failure_marks_stage_and_run_error_without_leaking_message(
    span_exporter, document, content
):
    span_exporter.clear()

    class FailingParser(FakeParser):
        def parse(self, filename, content):
            raise RuntimeError("secret-parse-failure private detail")

    service = _make_service(
        document=document,
        content=content,
        document_parser=FailingParser(),
    )

    with pytest.raises(RuntimeError, match="secret-parse-failure"):
        service.index_document("doc-1")

    spans = _index_spans(span_exporter)
    names = [span.name for span in spans]
    assert "nkb.index.parse" in names
    assert "nkb.index.split" not in names
    parse = next(span for span in spans if span.name == "nkb.index.parse")
    assert parse.attributes["nkb.outcome"] == "error"
    assert parse.attributes["error.type"] == "other"
    run = next(span for span in spans if span.name == "nkb.index.run")
    assert run.attributes["nkb.outcome"] == "error"
    dumped = json.dumps([(span.name, span.attributes) for span in spans], default=str)
    assert "secret-parse-failure" not in dumped
    assert "private detail" not in dumped


def test_rollback_failure_counts_error_once(span_exporter, document, content):
    span_exporter.clear()

    class FlakyChunkRepository(FakeChunkRepository):
        def __init__(self):
            super().__init__()
            self.replace_calls = 0

        def replace_chunks(self, document_id, chunks):
            self.replace_calls += 1
            if self.replace_calls >= 2:
                raise RuntimeError("restore_failed_secret")
            self.replaced.append((document_id, list(chunks)))

    class FailingVectorIndex(FakeVectorIndex):
        def replace_document(self, document_id, chunks, vectors, tags, source):
            raise RuntimeError("qdrant_unavailable")

    chunk_repository = FlakyChunkRepository()
    service = _make_service(
        document=document,
        content=content,
        chunk_repository=chunk_repository,
        vector_index=FailingVectorIndex(),
    )

    with pytest.raises(RuntimeError, match="qdrant_unavailable"):
        service.index_document("doc-1")

    assert chunk_repository.replace_calls == 2
    spans = _index_spans(span_exporter)
    write = next(span for span in spans if span.name == "nkb.index.write")
    assert write.attributes["nkb.outcome"] == "error"
    run = next(span for span in spans if span.name == "nkb.index.run")
    assert run.attributes["nkb.outcome"] == "error"
    assert "nkb.chunk_count" not in run.attributes


def test_cancelled_during_embed_propagates_and_counts_once(
    span_exporter, document, content, monkeypatch
):
    span_exporter.clear()

    class CancellingEmbeddingProvider(FakeEmbeddingProvider):
        def embed_chunks(self, chunks):
            raise asyncio.CancelledError()

    calls = []

    def spy(outcome, **kwargs):
        calls.append((outcome, kwargs))

    monkeypatch.setattr(ts, "record_index", spy)
    document_repository = FakeDocumentRepository(
        document=document, content=content, tags=[]
    )
    service = _make_service(
        document_repository=document_repository,
        embedding_provider=CancellingEmbeddingProvider(),
    )

    with pytest.raises(asyncio.CancelledError):
        service.index_document("doc-1")

    assert len(calls) == 1
    assert calls[0][0] == ts.OUTCOME_CANCELLED
    spans = _index_spans(span_exporter)
    embed = next(span for span in spans if span.name == "nkb.index.embed")
    assert embed.attributes["nkb.outcome"] == "cancelled"
    run = next(span for span in spans if span.name == "nkb.index.run")
    assert run.attributes["nkb.outcome"] == "cancelled"
    assert "error.type" not in run.attributes
    # 取消不进入既有 except Exception 失败收尾：状态停留在 INDEXING。
    assert document_repository.status_updates == [("doc-1", DocumentStatus.INDEXING)]


def test_index_helper_fault_does_not_change_business_result(
    document, content, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(ts.trace, "get_tracer", broken)
    monkeypatch.setattr(ts, "record_index", broken)
    service = _make_service(document=document, content=content)

    job = service.index_document("doc-1")

    assert job.status == IndexJobStatus.SUCCEEDED
