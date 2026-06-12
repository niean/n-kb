from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    ordinal: int
    text: str
    content_hash: str
    token_count: int
    metadata: dict[str, Any]
