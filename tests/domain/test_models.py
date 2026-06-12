from datetime import datetime, timezone

import pytest

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.embedding import EmbeddingVector
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.retrieval import RetrievalQuery, RetrievalResult
from app.domain.tag import Tag, parse_tags


def test_parse_tags_returns_key_value_tags_in_order():
    assert parse_tags("category=prd,project=n-agent,topic=rag") == [
        Tag("category", "prd"),
        Tag("project", "n-agent"),
        Tag("topic", "rag"),
    ]


def test_parse_tags_rejects_malformed_segments():
    with pytest.raises(ValueError, match="invalid tag"):
        parse_tags("category=prd,broken")


def test_document_models_hold_metadata_without_framework_types():
    now = datetime.now(timezone.utc)
    source = DocumentSource(
        kind="upload",
        uri="file://prd.md",
        display_name="PRD",
        metadata={"project": "n-agent"},
    )
    document = Document(
        id="doc-1",
        title="Product Requirements",
        source=source,
        content_hash="sha256:abc",
        content_type="text/markdown",
        size_bytes=123,
        status=DocumentStatus.UPLOADED,
        created_at=now,
        updated_at=now,
    )
    content = DocumentContent(
        document_id="doc-1",
        text="# Product Requirements",
        content_hash="sha256:abc",
        encoding="utf-8",
        created_at=now,
    )

    assert document.source.metadata == {"project": "n-agent"}
    assert document.status == DocumentStatus.UPLOADED
    assert document.status.value == "uploaded"
    assert content.document_id == document.id
    assert content.text == "# Product Requirements"


def test_retrieval_query_defaults_and_result_shape_include_snippet():
    query = RetrievalQuery(query="rag service")
    result = RetrievalResult(
        document_id="doc-1",
        chunk_id="chunk-1",
        score=0.91,
        snippet="RAG service snippet",
        source={"kind": "upload", "uri": "file://prd.md"},
        tags={"category": "prd"},
        metadata={"ordinal": 0},
    )

    assert query.top_k == 5
    assert query.min_score is None
    assert query.filters.tags == {}
    assert result.snippet == "RAG service snippet"
    assert result.source["kind"] == "upload"


def test_index_job_chunk_and_embedding_vector_basic_fields():
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
    chunk = Chunk(
        id="chunk-1",
        document_id="doc-1",
        ordinal=0,
        text="Chunk text",
        content_hash="sha256:def",
        token_count=2,
        metadata={"heading": "Intro"},
    )
    vector = EmbeddingVector(
        chunk_id="chunk-1",
        model="bge-m3",
        dimensions=3,
        values=[0.1, 0.2, 0.3],
    )

    assert job.status == IndexJobStatus.PENDING
    assert job.stage == IndexStage.CREATED
    assert chunk.ordinal == 0
    assert chunk.metadata == {"heading": "Intro"}
    assert vector.chunk_id == chunk.id
    assert vector.dimensions == 3
    assert vector.values == [0.1, 0.2, 0.3]
