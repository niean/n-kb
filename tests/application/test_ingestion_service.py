from dataclasses import replace
from datetime import datetime, timezone

import pytest

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
