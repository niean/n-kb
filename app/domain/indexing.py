from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IndexJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IndexStage(StrEnum):
    CREATED = "created"
    PARSING = "parsing"
    SPLITTING = "splitting"
    EMBEDDING = "embedding"
    WRITING_VECTOR_INDEX = "writing_vector_index"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class IndexJob:
    id: str
    document_id: str
    status: IndexJobStatus
    stage: IndexStage
    error: str | None
    created_at: datetime
    updated_at: datetime
