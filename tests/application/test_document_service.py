import hashlib
from pathlib import Path

import pytest

from app.application.document_service import DocumentService, UploadDocumentCommand
from app.domain.chunk import Chunk
from app.domain.document import DocumentStatus
from app.domain.ports import StoredObject
from app.domain.tag import Tag


class FakeDocumentRepository:
    def __init__(self):
        self.documents = {}
        self.contents = {}
        self.tags = {}
        self.chunks = {}
        self.saved = []

    def save_document(self, document, content, tags):
        self.documents[document.id] = document
        self.contents[document.id] = content
        self.tags[document.id] = tags
        self.saved.append((document, content, tags))

    def get_document(self, document_id):
        return self.documents.get(document_id)

    def find_by_content_hash(self, content_hash):
        for document in self.documents.values():
            if document.content_hash == content_hash:
                return document
        return None

    def get_content(self, document_id):
        return self.contents.get(document_id)

    def list_documents(self, tags=None, status=None):
        documents = list(self.documents.values())
        if status is not None:
            documents = [document for document in documents if document.status == status]
        if tags:
            documents = [
                document
                for document in documents
                if {tag.key: tag.value for tag in self.tags[document.id]}.items() >= tags.items()
            ]
        return documents

    def get_tags(self, document_id):
        return self.tags.get(document_id, [])

    def list_chunks(self, document_id):
        return self.chunks.get(document_id, [])

    def update_status(self, document_id, status):
        document = self.documents[document_id]
        self.documents[document_id] = document.__class__(
            id=document.id,
            title=document.title,
            source=document.source,
            content_hash=document.content_hash,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            status=status,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )

    def delete_document(self, document_id):
        self.documents.pop(document_id, None)
        self.contents.pop(document_id, None)
        self.tags.pop(document_id, None)


class FakeObjectStore:
    def __init__(self):
        self.saved = []

    def save(self, document_id, filename, content):
        self.saved.append((document_id, filename, content))
        return StoredObject(
            path=Path("/tmp") / document_id / filename,
            size_bytes=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            content_type="text/markdown" if filename.endswith(".md") else "text/plain",
        )

    def read_text(self, document_id):
        return "stored text"

    def delete(self, document_id):
        pass


@pytest.fixture
def repository():
    return FakeDocumentRepository()


@pytest.fixture
def object_store():
    return FakeObjectStore()


@pytest.fixture
def service(repository, object_store):
    return DocumentService(
        document_repository=repository,
        object_store=object_store,
        max_upload_bytes=32,
    )


def test_upload_document_persists_metadata_content_tags_and_file(service, repository, object_store):
    command = UploadDocumentCommand(
        filename="prd.md",
        content=b"# PRD\ncontent",
        source="Product Requirements",
        tags="category=prd,project=n-kb",
    )

    document = service.upload_document(command)

    assert document.title == "prd.md"
    assert document.status == DocumentStatus.UPLOADED
    assert document.source.kind == "upload"
    assert document.source.uri == "prd.md"
    assert document.source.display_name == "Product Requirements"
    assert document.source.metadata == {}
    assert document.content_hash == hashlib.sha256(command.content).hexdigest()
    assert document.size_bytes == len(command.content)
    assert object_store.saved == [(document.id, "prd.md", command.content)]

    saved_document, saved_content, saved_tags = repository.saved[0]
    assert saved_document == document
    assert saved_content.document_id == document.id
    assert saved_content.text == "# PRD\ncontent"
    assert saved_content.content_hash == document.content_hash
    assert saved_content.encoding == "utf-8"
    assert saved_tags == [Tag("category", "prd"), Tag("project", "n-kb")]


def test_upload_document_uses_filename_as_source_display_name_when_source_missing(service):
    document = service.upload_document(
        UploadDocumentCommand(
            filename="notes.txt",
            content=b"hello",
            source=None,
            tags=None,
        )
    )

    assert document.source.display_name == "notes.txt"


def test_upload_document_returns_existing_document_for_same_content(service, repository, object_store):
    command = UploadDocumentCommand(
        filename="prd.md",
        content=b"# PRD\ncontent",
        source="Product Requirements",
        tags="category=prd,project=n-kb",
    )

    first = service.upload_document(command)
    second = service.upload_document(command)

    assert second == first
    assert service.list_documents() == [first]
    assert len(repository.saved) == 1
    assert object_store.saved == [(first.id, "prd.md", command.content)]


def test_upload_document_rejects_unsupported_file_extension(service):
    with pytest.raises(ValueError, match="unsupported_file_type"):
        service.upload_document(
            UploadDocumentCommand(
                filename="image.png",
                content=b"not text",
                source=None,
                tags=None,
            )
        )


def test_upload_document_rejects_content_larger_than_max_upload_bytes(service):
    with pytest.raises(ValueError, match="file_too_large"):
        service.upload_document(
            UploadDocumentCommand(
                filename="large.md",
                content=b"x" * 33,
                source=None,
                tags=None,
            )
        )


def test_upload_document_rejects_non_utf8_content(service):
    with pytest.raises(ValueError, match="unsupported_file_type"):
        service.upload_document(
            UploadDocumentCommand(
                filename="broken.md",
                content=b"\xff\xfe",
                source=None,
                tags=None,
            )
        )


def test_document_query_helpers_delegate_to_repository(service, repository):
    uploaded = service.upload_document(
        UploadDocumentCommand(
            filename="doc.md",
            content=b"body",
            source=None,
            tags="topic=rag",
        )
    )

    assert service.list_documents(tags={"topic": "rag"}, status=DocumentStatus.UPLOADED) == [uploaded]
    assert service.get_document(uploaded.id) == uploaded
    assert service.get_content(uploaded.id).text == "body"
    assert service.get_tags(uploaded.id) == [Tag("topic", "rag")]


def test_list_chunks_returns_document_chunks(service, repository):
    uploaded = service.upload_document(
        UploadDocumentCommand(
            filename="doc.md",
            content=b"body",
            source=None,
            tags=None,
        )
    )
    repository.chunks[uploaded.id] = [Chunk("chunk-1", uploaded.id, 0, "body", "hash", 1, {"ordinal": 0})]

    assert service.list_chunks(uploaded.id) == repository.chunks[uploaded.id]


def test_list_chunks_missing_document_raises_document_not_found(service):
    with pytest.raises(ValueError, match="document_not_found"):
        service.list_chunks("missing")


class FakeVectorIndex:
    def __init__(self):
        self.replaced = []

    def replace_document(self, document_id, chunks, vectors, tags, source):
        self.replaced.append((document_id, chunks, vectors, tags, source))

    def search(self, vector, filters, top_k):
        return []

    def health(self):
        return {"status": "ok"}


class DeletingObjectStore(FakeObjectStore):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def delete(self, document_id):
        self.deleted.append(document_id)


def test_delete_document_cleans_vectors_files_and_repository(repository):
    object_store = DeletingObjectStore()
    vector_index = FakeVectorIndex()
    service = DocumentService(
        document_repository=repository,
        object_store=object_store,
        max_upload_bytes=32,
        vector_index=vector_index,
    )
    uploaded = service.upload_document(
        UploadDocumentCommand(
            filename="doc.md",
            content=b"body",
            source=None,
            tags="topic=rag",
        )
    )

    service.delete_document(uploaded.id)

    assert vector_index.replaced == [(uploaded.id, [], [], {}, {})]
    assert object_store.deleted == [uploaded.id]
    assert service.get_document(uploaded.id) is None


def test_delete_missing_document_raises_document_not_found(service):
    with pytest.raises(ValueError, match="document_not_found"):
        service.delete_document("missing")
