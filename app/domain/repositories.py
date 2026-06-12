from typing import Protocol

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag


class DocumentRepository(Protocol):
    def save_document(self, document: Document, content: DocumentContent, tags: list[Tag]) -> None: ...

    def get_document(self, document_id: str) -> Document | None: ...

    def get_content(self, document_id: str) -> DocumentContent | None: ...

    def list_documents(
        self,
        tags: dict[str, str] | None = None,
        status: DocumentStatus | None = None,
    ) -> list[Document]: ...

    def get_tags(self, document_id: str) -> list[Tag]: ...

    def update_status(self, document_id: str, status: DocumentStatus) -> None: ...

    def delete_document(self, document_id: str) -> None: ...


class ChunkRepository(Protocol):
    def replace_chunks(self, document_id: str, chunks: list[Chunk]) -> None: ...

    def list_chunks(self, document_id: str) -> list[Chunk]: ...


class IndexJobRepository(Protocol):
    def create_job(self, job: IndexJob) -> None: ...

    def get_job(self, job_id: str) -> IndexJob | None: ...

    def update_job(
        self,
        job_id: str,
        status: IndexJobStatus,
        stage: IndexStage,
        error: str | None = None,
    ) -> None: ...
