from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlite3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.instrumentation.dbapi import wrap_connect
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.application import telemetry_semantics as ts
from app.application.document_service import UPLOAD_SPAN_NAME, DocumentService
from app.application.ingestion_service import IngestionService
from app.config import Settings
from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider
from app.infrastructure.observability.instrumentors import instrument_app
from app.infrastructure.parsers.text_parser import SimpleTextParser, SimpleTextSplitter
from app.infrastructure.persistence.sqlite_store import SQLiteStore
from app.infrastructure.storage.local_object_store import LocalObjectStore
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
        "created_at": "2026-01-02T00:00:00Z",
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

    client = TestClient(app)

    # T7：services 由 lifespan 填充；启动后在首次请求前替换观测依赖。
    with client:
        app.state.services["ingestion"]._embedding_provider = FakeEmbeddingProvider()
        app.state.services["ingestion"]._vector_index = FakeVectorIndex()

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

    # T7：services 由 lifespan 填充。
    with client:
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


# ---------------------------------------------------------------------------
# T8: 上传入库链路 API 集成 span 树（SERVER -> upload + nkb.index.run -> 四阶段
#     -> embed -> HTTPX，且 Qdrant REST 与 SQLite 子 span 挂在真实调用点）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def otel_capture():
    """T2/T7 既定模式：附加到现有全局 SDK provider，或设置自有 provider。"""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = trace.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(processor)
        trace.set_tracer_provider(owning)
        provider = owning
    else:
        try:
            provider.add_span_processor(processor)
        except Exception:
            pass
    return SimpleNamespace(provider=provider, exporter=exporter)


_SQLITE_WRAPPED = False


def _wrap_sqlite_once(provider):
    """本模块对 sqlite3.connect 追加一次 dbapi 埋点（与 T4 全局注册可叠加）。"""
    global _SQLITE_WRAPPED
    if _SQLITE_WRAPPED:
        return
    _SQLITE_WRAPPED = True
    wrap_connect(
        "t8-documents-tests",
        sqlite3,
        "connect",
        "sqlite",
        {},
        version="t8-tests",
        tracer_provider=provider,
    )


def _instrumented_client(handler, tracer_provider):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider)
    return client


class _HttpQdrantIndex:
    def __init__(self, tracer_provider):
        self.client = _instrumented_client(
            lambda request: httpx.Response(200, json={}), tracer_provider
        )

    def replace_document(self, document_id, chunks, vectors, tags, source):
        response = self.client.put(
            "http://qdrant.test/collections/n-kb/points", json={"points": []}
        )
        response.raise_for_status()

    def search(self, vector, filters, top_k):
        return []

    def health(self):
        return {"status": "ok"}


def _build_traced_upload_app(tmp_path, otel_capture):
    _wrap_sqlite_once(otel_capture.provider)
    embedding_provider = OllamaEmbeddingProvider(
        base_url="http://ollama.test",
        model="bge-m3",
        client=_instrumented_client(
            lambda request: httpx.Response(
                200,
                json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 3},
            ),
            otel_capture.provider,
        ),
        embedding_observer=ts.embedding_observer,
    )
    qdrant = _HttpQdrantIndex(otel_capture.provider)
    sqlite_store = SQLiteStore(tmp_path / "n-kb.db")
    documents = DocumentService(
        document_repository=sqlite_store,
        object_store=LocalObjectStore(tmp_path / "data"),
        max_upload_bytes=1024,
        vector_index=qdrant,
    )
    ingestion = IngestionService(
        document_repository=sqlite_store,
        chunk_repository=sqlite_store,
        index_job_repository=sqlite_store,
        document_parser=SimpleTextParser(),
        text_splitter=SimpleTextSplitter(),
        embedding_provider=embedding_provider,
        vector_index=qdrant,
    )
    app = FastAPI()
    app.state.services = {"documents": documents, "ingestion": ingestion}
    register_routes(app)
    assert instrument_app(
        app, settings=SimpleNamespace(mcp_path="/mcp"), tracer_provider=otel_capture.provider
    )
    return TestClient(app)


def test_upload_via_api_emits_server_upload_and_index_span_tree(tmp_path, otel_capture):
    otel_capture.exporter.clear()
    client = _build_traced_upload_app(tmp_path, otel_capture)

    with client:
        response = client.post(
            "/documents",
            files={"file": ("note.md", b"hello rag secret content", "text/markdown")},
            data={"source": "Manual", "tags": "topic=rag"},
        )

    assert response.status_code == 200
    document_id = response.json()["id"]

    spans = otel_capture.exporter.get_finished_spans()
    servers = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(servers) == 1
    server = servers[0]
    assert server.name == "POST /documents"

    # 同一个 SERVER 下同时有上传操作 span 与 nkb.index.run（不能只测独立 index 路由）。
    uploads = [s for s in spans if s.name == UPLOAD_SPAN_NAME]
    runs = [s for s in spans if s.name == ts.INDEX_RUN_SPAN_NAME]
    assert len(uploads) == 1 and len(runs) == 1
    upload, run = uploads[0], runs[0]
    assert upload.parent.span_id == server.context.span_id
    assert run.parent.span_id == server.context.span_id
    assert upload.context.trace_id == server.context.trace_id
    assert run.context.trace_id == server.context.trace_id
    assert upload.attributes[ts.NKB_OUTCOME] == ts.OUTCOME_SUCCESS

    # 四个阶段 span 依次挂在 run 下，顺序 parse/split/embed/write。
    stage_names = (ts.INDEX_STAGE_SPAN_PREFIX + stage for stage in ts.INDEX_STAGES)
    stage_spans = {name: [s for s in spans if s.name == name] for name in stage_names}
    assert all(len(found) == 1 for found in stage_spans.values())
    for name, found in stage_spans.items():
        assert found[0].parent.span_id == run.context.span_id
        assert found[0].attributes[ts.NKB_OUTCOME] == ts.OUTCOME_SUCCESS
    run_children = [s for s in spans if s.parent is not None and s.parent.span_id == run.context.span_id]
    ordered_stages = [
        s for s in run_children if s.name.startswith(ts.INDEX_STAGE_SPAN_PREFIX)
    ]
    ordered_stages.sort(key=lambda s: s.start_time)
    assert [s.name for s in ordered_stages] == [
        ts.INDEX_STAGE_SPAN_PREFIX + stage for stage in ts.INDEX_STAGES
    ]

    # embed 阶段 -> GenAI CLIENT -> HTTPX POST；write 阶段 -> Qdrant REST HTTPX PUT。
    embed_stage = stage_spans[ts.INDEX_STAGE_SPAN_PREFIX + "embed"][0]
    write_stage = stage_spans[ts.INDEX_STAGE_SPAN_PREFIX + "write"][0]
    embeddings = [
        s for s in spans if s.kind == SpanKind.CLIENT and s.name == "embeddings bge-m3"
    ]
    assert len(embeddings) == 1
    assert embeddings[0].parent.span_id == embed_stage.context.span_id
    assert embeddings[0].attributes[ts.GEN_AI_REQUEST_MODEL] == "bge-m3"
    httpx_spans = [s for s in spans if s.kind == SpanKind.CLIENT and s.name in ("POST", "PUT")]
    ollama_calls = [s for s in httpx_spans if s.attributes.get("http.url") == "http://ollama.test/api/embed"]
    qdrant_calls = [s for s in httpx_spans if s.attributes.get("http.url") == "http://qdrant.test/collections/n-kb/points"]
    assert len(ollama_calls) == 1 and len(qdrant_calls) == 1
    assert ollama_calls[0].parent.span_id == embeddings[0].context.span_id
    assert qdrant_calls[0].parent.span_id == write_stage.context.span_id

    # SQLite 子 span 挂在真实调用点：run 下的 job/状态写入与 write 阶段下的
    # chunk 替换均为 db.system=sqlite CLIENT。
    sqlite_spans = [
        s for s in spans if s.kind == SpanKind.CLIENT and s.attributes.get("db.system") == "sqlite"
    ]
    assert sqlite_spans, "expected SQLite CLIENT spans"
    run_sqlite = [s for s in sqlite_spans if s.parent.span_id == run.context.span_id]
    write_sqlite = [s for s in sqlite_spans if s.parent.span_id == write_stage.context.span_id]
    assert run_sqlite, "expected SQLite spans directly under nkb.index.run"
    assert write_sqlite, "expected SQLite spans under nkb.index.write stage"

    # run span 携带安全结果：document_id 与成功 chunk 数。
    assert run.attributes[ts.NKB_DOCUMENT_ID] == document_id
    assert run.attributes[ts.NKB_CHUNK_COUNT] == 1
    assert run.attributes[ts.NKB_OUTCOME] == ts.OUTCOME_SUCCESS

    # 同 trace、span id 唯一。
    assert {s.context.trace_id for s in spans} == {server.context.trace_id}
    span_ids = [s.context.span_id for s in spans]
    assert len(span_ids) == len(set(span_ids))

    # 安全投影：文档正文/标签值不进入任何 span 属性或事件。
    dumped = "".join(_span_dump(s) for s in spans)
    assert "hello rag secret content" not in dumped
    assert "rag" not in dumped.replace("n-kb", "").replace("qdrant", "")


def _span_dump(span):
    import json

    return json.dumps(
        {
            "name": span.name,
            "attributes": dict(span.attributes or {}),
            "events": [dict(event.attributes or {}) for event in span.events],
        },
        default=str,
    )
