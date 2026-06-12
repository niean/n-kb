from datetime import datetime, timezone
import uuid

from app.domain.document import DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.ports import DocumentParser, EmbeddingProvider, TextSplitter, VectorIndex
from app.domain.repositories import ChunkRepository, DocumentRepository, IndexJobRepository


class IngestionService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        chunk_repository: ChunkRepository,
        index_job_repository: IndexJobRepository,
        document_parser: DocumentParser,
        text_splitter: TextSplitter,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
    ) -> None:
        self._document_repository = document_repository
        self._chunk_repository = chunk_repository
        self._index_job_repository = index_job_repository
        self._document_parser = document_parser
        self._text_splitter = text_splitter
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index

    def index_document(self, document_id: str) -> IndexJob:
        document = self._document_repository.get_document(document_id)
        content = self._document_repository.get_content(document_id)
        if document is None or content is None:
            raise ValueError("document_not_found")

        now = datetime.now(timezone.utc)
        job = IndexJob(
            id=uuid.uuid4().hex,
            document_id=document_id,
            status=IndexJobStatus.PENDING,
            stage=IndexStage.CREATED,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._index_job_repository.create_job(job)

        try:
            self._document_repository.update_status(document_id, DocumentStatus.INDEXING)

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.PARSING)
            parsed_text = self._document_parser.parse(
                document.title,
                content.text.encode(content.encoding or "utf-8"),
            )

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.SPLITTING)
            chunks = self._text_splitter.split(document_id, parsed_text)

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.EMBEDDING)
            vectors = self._embedding_provider.embed_chunks(chunks)
            self._validate_embedding_vectors(chunks, vectors)

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.WRITING_VECTOR_INDEX)
            tags = {tag.key: tag.value for tag in self._document_repository.get_tags(document_id)}
            source = {"kind": document.source.kind, "uri": document.source.uri}
            previous_chunks = self._chunk_repository.list_chunks(document_id)
            self._chunk_repository.replace_chunks(document_id, chunks)
            try:
                self._vector_index.replace_document(document_id, chunks, vectors, tags, source)
            except Exception as vector_exc:
                try:
                    self._chunk_repository.replace_chunks(document_id, previous_chunks)
                except Exception as restore_exc:
                    raise vector_exc from restore_exc
                raise

            self._document_repository.update_status(document_id, DocumentStatus.INDEXED)
            self._update_job(job.id, IndexJobStatus.SUCCEEDED, IndexStage.COMPLETED)
            final_job = self._index_job_repository.get_job(job.id)
        except Exception as exc:
            self._document_repository.update_status(document_id, DocumentStatus.FAILED)
            self._update_job(
                job.id,
                IndexJobStatus.FAILED,
                IndexStage.FAILED,
                self._safe_error_summary(exc),
            )
            raise

        if final_job is None:
            raise RuntimeError("index_job_not_found")
        return final_job

    def _validate_embedding_vectors(self, chunks, vectors) -> None:
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding_vector_mismatch")
        for chunk, vector in zip(chunks, vectors, strict=True):
            if vector.chunk_id != chunk.id:
                raise RuntimeError("embedding_vector_mismatch")

    def _safe_error_summary(self, exc: Exception) -> str:
        message = str(exc)
        stable_codes = {"document_not_found", "embedding_vector_mismatch"}
        if message in stable_codes:
            return message
        return exc.__class__.__name__

    def _update_job(
        self,
        job_id: str,
        status: IndexJobStatus,
        stage: IndexStage,
        error: str | None = None,
    ) -> None:
        self._index_job_repository.update_job(job_id, status, stage, error)
