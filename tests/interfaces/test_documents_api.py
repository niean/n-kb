from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag
from app.main import create_app
from app.interfaces.http.routes import register_routes


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def make_document(document_id: str = "doc-1", status: DocumentStatus = DocumentStatus.UPLOADED) -> Document:
    return Document(
        id=document_id,
        title="note.md",
        source=DocumentSource(kind="upload", uri="note.md", display_name="Manual", metadata={}),
        content_hash="hash",
        content_type="text/markdown",
        size_bytes=12,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


@dataclass
class FakeUploadCommandCapture:
    filename: str | None = None
    content: bytes | None = None
    source: str | None = None
    tags: str | None = None


class FakeDocumentService:
    def __init__(self):
        self.capture = FakeUploadCommandCapture()
        self.documents = [make_document()]
        self.tags = {"doc-1": [Tag("topic", "rag")]}
        self.contents = {"doc-1": DocumentContent("doc-1", "hello rag", "hash", "utf-8", NOW)}
        self.chunks = {}
        self.list_call = None
        self.max_upload_bytes = 1024
        self.upload_call_count = 0
        self.deleted_document_id = None

    def upload_document(self, command):
        self.upload_call_count += 1
        self.capture = FakeUploadCommandCapture(
            filename=command.filename,
            content=command.content,
            source=command.source,
            tags=command.tags,
        )
        return make_document("uploaded-1")

    def list_documents(self, tags=None, status=None):
        self.list_call = {"tags": tags, "status": status}
        return self.documents

    def get_document(self, document_id):
        return make_document(document_id) if document_id == "doc-1" else None

    def get_content(self, document_id):
        return self.contents.get(document_id)

    def get_tags(self, document_id):
        return self.tags.get(document_id, [])

    def list_chunks(self, document_id):
        if document_id != "doc-1":
            raise ValueError("document_not_found")
        return self.chunks.get(document_id, [])

    def delete_document(self, document_id):
        self.deleted_document_id = document_id


class FakeIngestionService:
    def __init__(self):
        self.jobs = {
            "job-1": IndexJob("job-1", "doc-1", IndexJobStatus.SUCCEEDED, IndexStage.COMPLETED, None, NOW, NOW)
        }
        self.indexed_document_id = None

    def index_document(self, document_id):
        self.indexed_document_id = document_id
        return IndexJob("job-2", document_id, IndexJobStatus.PENDING, IndexStage.CREATED, None, NOW, NOW)

    def get_job(self, job_id):
        return self.jobs.get(job_id)


def build_client(document_service=None, ingestion_service=None) -> TestClient:
    app = FastAPI()
    app.state.services = {
        "documents": document_service or FakeDocumentService(),
        "ingestion": ingestion_service or FakeIngestionService(),
    }
    register_routes(app)
    return TestClient(app)


def test_upload_document_returns_stable_document_json_and_passes_multipart_fields():
    service = FakeDocumentService()
    client = build_client(document_service=service)

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": "uploaded-1",
        "title": "note.md",
        "status": "uploaded",
        "source": {"kind": "upload", "uri": "note.md", "display_name": "Manual"},
        "tags": {},
    }
    assert service.capture.filename == "note.md"
    assert service.capture.content == b"hello rag"
    assert service.capture.source == "Manual"
    assert service.capture.tags == "topic=rag"


def test_upload_document_indexes_uploaded_document():
    document_service = FakeDocumentService()
    ingestion_service = FakeIngestionService()
    client = build_client(document_service=document_service, ingestion_service=ingestion_service)

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 200
    assert ingestion_service.indexed_document_id == "uploaded-1"


def test_upload_maps_validation_codes_to_stable_error_payloads():
    class RejectingDocumentService(FakeDocumentService):
        def upload_document(self, command):
            raise ValueError("unsupported_file_type")

    client = build_client(document_service=RejectingDocumentService())

    response = client.post(
        "/documents",
        files={"file": ("note.pdf", b"pdf", "application/pdf")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "unsupported_file_type", "message": "unsupported_file_type"}}



def test_upload_rejects_oversized_file_without_calling_service():
    service = FakeDocumentService()
    service.max_upload_bytes = 4
    client = build_client(document_service=service)

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "file_too_large", "message": "file_too_large"}}
    assert service.upload_call_count == 0


def test_create_app_upload_indexes_document_immediately(tmp_path):
    settings = Settings(
        sqlite_path=tmp_path / "n-kb.db",
        storage_root=tmp_path / "data",
    )
    app = create_app(settings)

    class FakeEmbeddingProvider:
        def embed_chunks(self, chunks):
            from app.domain.embedding import EmbeddingVector

            return [
                EmbeddingVector(chunk_id=chunk.id, model="fake", dimensions=2, values=[1.0, 0.0])
                for chunk in chunks
            ]

        def embed_query(self, query):
            return [1.0, 0.0]

    class FakeVectorIndex:
        def replace_document(self, document_id, chunks, vectors, tags, source):
            self.replacement = (document_id, chunks, vectors, tags, source)

        def search(self, vector, filters, top_k):
            return []

        def health(self):
            return {"status": "ok"}

    app.state.services["ingestion"]._embedding_provider = FakeEmbeddingProvider()
    app.state.services["ingestion"]._vector_index = FakeVectorIndex()
    client = TestClient(app)

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 200
    document_id = response.json()["id"]
    assert client.get(f"/documents/{document_id}").json()["status"] == "indexed"


def test_create_app_upload_guard_uses_configured_max_upload_bytes(tmp_path):
    settings = Settings(
        sqlite_path=tmp_path / "n-kb.db",
        storage_root=tmp_path / "data",
        max_upload_bytes=4,
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert app.state.settings.max_upload_bytes == 4
    assert app.state.services["documents"].max_upload_bytes == 4
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "file_too_large", "message": "file_too_large"}}
    assert client.get("/documents").json() == []



def test_unknown_value_error_maps_to_validation_error_without_raw_message():
    class RejectingDocumentService(FakeDocumentService):
        def upload_document(self, command):
            raise ValueError("raw private validation details")

    client = build_client(document_service=RejectingDocumentService())

    response = client.post(
        "/documents",
        files={"file": ("note.md", b"hello rag", "text/markdown")},
        data={"source": "Manual", "tags": "topic=rag"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "validation_error", "message": "validation_error"}}


def test_list_documents_supports_tags_and_status_filters():
    service = FakeDocumentService()
    client = build_client(document_service=service)

    response = client.get("/documents", params={"tags": "topic=rag,kind=spec", "status": "indexed"})

    assert response.status_code == 200
    assert response.json()[0]["id"] == "doc-1"
    assert service.list_call == {"tags": {"topic": "rag", "kind": "spec"}, "status": DocumentStatus.INDEXED}


def test_get_document_includes_tags_and_content_endpoint_returns_text():
    client = build_client()

    detail = client.get("/documents/doc-1")
    content = client.get("/documents/doc-1/content")

    assert detail.status_code == 200
    assert detail.json()["tags"] == {"topic": "rag"}
    assert content.status_code == 200
    assert content.json() == {"document_id": "doc-1", "text": "hello rag"}


def test_get_document_chunks_returns_ordered_chunk_json():
    service = FakeDocumentService()
    service.chunks["doc-1"] = [Chunk("chunk-1", "doc-1", 0, "hello rag", "hash", 2, {"ordinal": 0})]
    client = build_client(document_service=service)

    response = client.get("/documents/doc-1/chunks")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "chunk-1",
            "document_id": "doc-1",
            "ordinal": 0,
            "text": "hello rag",
            "token_count": 2,
            "metadata": {"ordinal": 0},
        }
    ]


def test_get_missing_document_chunks_returns_document_not_found():
    client = build_client()

    response = client.get("/documents/missing/chunks")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "document_not_found", "message": "document_not_found"}}


def test_missing_document_returns_document_not_found_error():
    client = build_client()

    response = client.get("/documents/missing")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "document_not_found", "message": "document_not_found"}}


def test_delete_document_delegates_to_document_service():
    service = FakeDocumentService()
    client = build_client(document_service=service)

    response = client.delete("/documents/doc-1")

    assert response.status_code == 204
    assert service.deleted_document_id == "doc-1"


def test_index_document_returns_job_json_and_job_lookup():
    ingestion = FakeIngestionService()
    client = build_client(ingestion_service=ingestion)

    created = client.post("/documents/doc-1/index")
    existing = client.get("/index-jobs/job-1")

    assert created.status_code == 200
    assert created.json()["document_id"] == "doc-1"
    assert created.json()["status"] == "pending"
    assert ingestion.indexed_document_id == "doc-1"
    assert existing.status_code == 200
    assert existing.json() == {
        "id": "job-1",
        "document_id": "doc-1",
        "status": "succeeded",
        "stage": "completed",
        "error": None,
    }


def test_indexing_runtime_error_returns_indexing_failed_without_stack_trace():
    class FailingIngestionService(FakeIngestionService):
        def index_document(self, document_id):
            raise RuntimeError("boom secret stack details")

    client = build_client(ingestion_service=FailingIngestionService())

    response = client.post("/documents/doc-1/index")

    assert response.status_code == 502
    assert response.json() == {"error": {"code": "indexing_failed", "message": "indexing_failed"}}
