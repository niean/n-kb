from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent
from app.domain.indexing import IndexJob
from app.domain.retrieval import RetrievalFilter, RetrievalResult
from app.domain.tag import Tag


class SourceResponse(BaseModel):
    kind: str
    uri: str
    display_name: str | None = None


class DocumentResponse(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    source: SourceResponse
    tags: dict[str, str] = Field(default_factory=dict)


class DocumentContentResponse(BaseModel):
    document_id: str
    text: str


class ChunkResponse(BaseModel):
    id: str
    document_id: str
    ordinal: int
    text: str
    token_count: int
    metadata: dict[str, Any]


class IndexJobResponse(BaseModel):
    id: str
    document_id: str
    status: str
    stage: str
    error: str | None = None


class RetrievalFiltersRequest(BaseModel):
    tags: dict[str, str] = Field(default_factory=dict)
    source_kind: str | None = None
    document_status: str | None = None


class RetrievalSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    filters: RetrievalFiltersRequest = Field(default_factory=RetrievalFiltersRequest)
    min_score: float = Field(default=0.5, ge=0, le=1)


class RetrievalResultResponse(BaseModel):
    document_id: str
    chunk_id: str
    score: float
    snippet: str
    source: dict[str, Any]
    tags: dict[str, str]
    metadata: dict[str, Any]


class RetrievalSearchResponse(BaseModel):
    query: str
    results: list[RetrievalResultResponse]


def tags_to_dict(tags: list[Tag] | None) -> dict[str, str]:
    return {tag.key: tag.value for tag in tags or []}


def document_to_response(document: Document, tags: list[Tag] | None = None) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        title=document.title,
        status=document.status.value,
        created_at=document.created_at,
        source=SourceResponse(
            kind=document.source.kind,
            uri=document.source.uri,
            display_name=document.source.display_name,
        ),
        tags=tags_to_dict(tags),
    )


def content_to_response(content: DocumentContent) -> DocumentContentResponse:
    return DocumentContentResponse(document_id=content.document_id, text=content.text)


def chunk_to_response(chunk: Chunk) -> ChunkResponse:
    return ChunkResponse(
        id=chunk.id,
        document_id=chunk.document_id,
        ordinal=chunk.ordinal,
        text=chunk.text,
        token_count=chunk.token_count,
        metadata=chunk.metadata,
    )


def job_to_response(job: IndexJob) -> IndexJobResponse:
    return IndexJobResponse(
        id=job.id,
        document_id=job.document_id,
        status=job.status.value,
        stage=job.stage.value,
        error=job.error,
    )


def request_filters_to_domain(filters: RetrievalFiltersRequest, min_score: float | None = None) -> RetrievalFilter:
    return RetrievalFilter(
        tags=filters.tags,
        source_kind=filters.source_kind,
        document_status=filters.document_status,
        min_score=min_score,
    )


def retrieval_result_to_response(result: RetrievalResult) -> RetrievalResultResponse:
    metadata = {key: value for key, value in result.metadata.items() if key != "vector"}
    return RetrievalResultResponse(
        document_id=result.document_id,
        chunk_id=result.chunk_id,
        score=result.score,
        snippet=result.snippet,
        source=result.source,
        tags=result.tags,
        metadata=metadata,
    )
