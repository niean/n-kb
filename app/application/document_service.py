from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.ports import ObjectStore, VectorIndex
from app.domain.repositories import ChunkRepository, DocumentRepository
from app.domain.tag import Tag, parse_tags


@dataclass(frozen=True)
class UploadDocumentCommand:
    filename: str
    content: bytes
    source: str | None
    tags: str | None


class DocumentService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        object_store: ObjectStore,
        max_upload_bytes: int,
        vector_index: VectorIndex | None = None,
        allowed_extensions: set[str] | None = None,
        chunk_repository: ChunkRepository | None = None,
    ) -> None:
        self._document_repository = document_repository
        self._chunk_repository = chunk_repository or document_repository
        self._object_store = object_store
        self._max_upload_bytes = max_upload_bytes
        self._vector_index = vector_index
        self._allowed_extensions = allowed_extensions or {".md", ".markdown", ".txt"}

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    def upload_document(self, command: UploadDocumentCommand) -> Document:
        self._validate_upload(command.filename, command.content)
        text = self._decode_content(command.content)
        tags = parse_tags(command.tags)
        content_hash = hashlib.sha256(command.content).hexdigest()
        existing_document = self._document_repository.find_by_content_hash(content_hash)
        if existing_document is not None:
            return existing_document

        document_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        stored_object = self._object_store.save(document_id, command.filename, command.content)

        source = DocumentSource(
            kind="upload",
            uri=command.filename,
            display_name=command.source or command.filename,
            metadata={},
        )
        document = Document(
            id=document_id,
            title=command.filename,
            source=source,
            content_hash=content_hash,
            content_type=stored_object.content_type,
            size_bytes=len(command.content),
            status=DocumentStatus.UPLOADED,
            created_at=now,
            updated_at=now,
        )
        content = DocumentContent(
            document_id=document_id,
            text=text,
            content_hash=content_hash,
            encoding="utf-8",
            created_at=now,
        )
        self._document_repository.save_document(document, content, tags)
        return document

    def list_documents(
        self,
        tags: dict[str, str] | None = None,
        status: DocumentStatus | None = None,
    ) -> list[Document]:
        return self._document_repository.list_documents(tags=tags, status=status)

    def get_document(self, document_id: str) -> Document | None:
        return self._document_repository.get_document(document_id)

    def get_content(self, document_id: str) -> DocumentContent | None:
        return self._document_repository.get_content(document_id)

    def get_tags(self, document_id: str) -> list[Tag]:
        return self._document_repository.get_tags(document_id)

    def list_chunks(self, document_id: str):
        if self._document_repository.get_document(document_id) is None:
            raise ValueError("document_not_found")
        return self._chunk_repository.list_chunks(document_id)

    def delete_document(self, document_id: str) -> None:
        document = self._document_repository.get_document(document_id)
        if document is None:
            raise ValueError("document_not_found")
        if self._vector_index is not None:
            self._vector_index.replace_document(document_id, [], [], tags={}, source={})
        self._object_store.delete(document_id)
        self._document_repository.delete_document(document_id)

    def _validate_upload(self, filename: str, content: bytes) -> None:
        if Path(filename).suffix.lower() not in self._allowed_extensions:
            raise ValueError("unsupported_file_type")
        if len(content) > self._max_upload_bytes:
            raise ValueError("file_too_large")

    @staticmethod
    def _decode_content(content: bytes) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("unsupported_file_type") from exc
