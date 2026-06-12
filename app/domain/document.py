from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass(frozen=True)
class DocumentSource:
    kind: str
    uri: str
    display_name: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    source: DocumentSource
    content_hash: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DocumentContent:
    document_id: str
    text: str
    content_hash: str
    encoding: str
    created_at: datetime
