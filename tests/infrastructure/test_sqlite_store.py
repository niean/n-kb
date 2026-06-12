from datetime import datetime, timezone

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag
from app.infrastructure.persistence.sqlite_store import SQLiteStore


def make_document(document_id="doc-1", status=DocumentStatus.UPLOADED):
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title="notes.md",
        source=DocumentSource(kind="upload", uri="notes.md", display_name="Notes", metadata={}),
        content_hash="hash-1",
        content_type="text/markdown",
        size_bytes=12,
        status=status,
        created_at=now,
        updated_at=now,
    )


def make_content(document_id="doc-1"):
    return DocumentContent(
        document_id=document_id,
        text="hello sqlite",
        content_hash="hash-1",
        encoding="utf-8",
        created_at=datetime.now(timezone.utc),
    )


def test_sqlite_store_initializes_schema_indexes_and_round_trips_document(tmp_path):
    store = SQLiteStore(tmp_path / "n-kb.db")
    document = make_document()
    content = make_content()

    store.save_document(document, content, [Tag("topic", "rag"), Tag("project", "n-kb")])

    assert store.health() == {"status": "ok"}
    assert store.get_document("doc-1") == document
    assert store.get_content("doc-1") == content
    assert store.get_tags("doc-1") == [Tag("project", "n-kb"), Tag("topic", "rag")]
    assert store.list_documents(tags={"topic": "rag"}, status=DocumentStatus.UPLOADED) == [document]
    assert store.list_documents(tags={"missing": "tag"}) == []

    with store._connect() as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(documents)")}
        tag_indexes = {row[1] for row in connection.execute("PRAGMA index_list(document_tags)")}
        chunk_indexes = {row[1] for row in connection.execute("PRAGMA index_list(chunks)")}
        job_indexes = {row[1] for row in connection.execute("PRAGMA index_list(index_jobs)")}

    assert "idx_documents_status" in indexes
    assert "idx_document_tags_key_value" in tag_indexes
    assert "idx_chunks_document_ordinal" in chunk_indexes
    assert "idx_index_jobs_document_created_at" in job_indexes


def test_sqlite_store_finds_document_by_content_hash(tmp_path):
    store = SQLiteStore(tmp_path / "n-kb.db")
    document = make_document()
    store.save_document(document, make_content(), [])

    assert store.find_by_content_hash("hash-1") == document
    assert store.find_by_content_hash("missing") is None


def test_replace_chunks_replaces_existing_chunks_in_ordinal_order(tmp_path):
    store = SQLiteStore(tmp_path / "n-kb.db")
    store.save_document(make_document(), make_content(), [])
    first_chunks = [
        Chunk("chunk-old", "doc-1", 0, "old", "old-hash", 1, {"ordinal": 0}),
    ]
    new_chunks = [
        Chunk("chunk-2", "doc-1", 1, "second", "hash-2", 1, {"ordinal": 1}),
        Chunk("chunk-1", "doc-1", 0, "first", "hash-1", 1, {"ordinal": 0}),
    ]

    store.replace_chunks("doc-1", first_chunks)
    store.replace_chunks("doc-1", new_chunks)

    assert [chunk.id for chunk in store.list_chunks("doc-1")] == ["chunk-1", "chunk-2"]
    assert store.list_chunks("missing") == []


def test_delete_document_removes_document_content_tags_chunks_and_jobs(tmp_path):
    store = SQLiteStore(tmp_path / "n-kb.db")
    store.save_document(make_document(), make_content(), [Tag("topic", "rag")])
    store.replace_chunks("doc-1", [Chunk("chunk-1", "doc-1", 0, "text", "hash", 1, {"ordinal": 0})])
    now = datetime.now(timezone.utc)
    store.create_job(IndexJob("job-1", "doc-1", IndexJobStatus.PENDING, IndexStage.CREATED, None, now, now))

    store.delete_document("doc-1")

    assert store.get_document("doc-1") is None
    assert store.get_content("doc-1") is None
    assert store.get_tags("doc-1") == []
    assert store.list_chunks("doc-1") == []
    assert store.get_job("job-1") is None



    store = SQLiteStore(tmp_path / "n-kb.db")
    now = datetime.now(timezone.utc)
    job = IndexJob(
        id="job-1",
        document_id="doc-1",
        status=IndexJobStatus.PENDING,
        stage=IndexStage.CREATED,
        error=None,
        created_at=now,
        updated_at=now,
    )

    store.create_job(job)
    store.update_job("job-1", IndexJobStatus.FAILED, IndexStage.FAILED, "RuntimeError")

    stored = store.get_job("job-1")
    assert stored is not None
    assert stored.id == "job-1"
    assert stored.status == IndexJobStatus.FAILED
    assert stored.stage == IndexStage.FAILED
    assert stored.error == "RuntimeError"
    assert stored.created_at == now
    assert stored.updated_at >= now
