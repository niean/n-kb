from typing import Protocol

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag


class DocumentRepository(Protocol):
    def save_document(self, document: Document, content: DocumentContent, tags: list[Tag]) -> None: ...

    def get_document(self, document_id: str) -> Document | None: ...

    def get_documents_by_ids(self, document_ids: list[str]) -> list[Document]:
        """按 ID 批量查询文档。

        返回命中的文档列表，缺失的 ID 不出现在结果中；实现方负责去重，
        并将单条 IN 语句的参数量约束在实现定义的安全上界内（分批执行）。
        """

    def find_by_content_hash(self, content_hash: str) -> Document | None: ...

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
