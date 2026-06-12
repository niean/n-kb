import hashlib
from pathlib import Path

from app.domain.chunk import Chunk


class SimpleTextParser:
    SUPPORTED_EXTENSIONS = {".md", ".txt"}

    def parse(self, filename: str, content: bytes) -> str:
        if Path(filename).suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            raise ValueError("unsupported_file_type")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("unsupported_file_type") from exc


class SimpleTextSplitter:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 100):
        if chunk_size <= 0:
            raise ValueError("chunk_size_must_be_positive")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("invalid_chunk_overlap")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, document_id: str, text: str) -> list[Chunk]:
        if not text:
            return []
        chunks: list[Chunk] = []
        start = 0
        ordinal = 0
        step = self.chunk_size - self.chunk_overlap
        while start < len(text):
            chunk_text = text[start : start + self.chunk_size]
            chunks.append(
                Chunk(
                    id=f"{document_id}-chunk-{ordinal}",
                    document_id=document_id,
                    ordinal=ordinal,
                    text=chunk_text,
                    content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                    token_count=len(chunk_text.split()),
                    metadata={"ordinal": ordinal},
                )
            )
            if start + self.chunk_size >= len(text):
                break
            start += step
            ordinal += 1
        return chunks
