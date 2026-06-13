from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.application.document_service import DocumentService
from app.application.health_service import HealthService
from app.application.ingestion_service import IngestionService
from app.application.retrieval_service import RetrievalService
from app.config import Settings
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider
from app.infrastructure.parsers.text_parser import SimpleTextParser, SimpleTextSplitter
from app.infrastructure.persistence.sqlite_store import SQLiteStore
from app.infrastructure.storage.local_object_store import LocalObjectStore
from app.infrastructure.vector.qdrant_index import QdrantVectorIndex
from app.interfaces.http.routes import register_routes
from app.interfaces.mcp.server import create_mcp_app


class _UnavailableQdrantClient:
    def get_collections(self):
        raise RuntimeError("qdrant_client_not_installed")


def _build_vector_index(settings: Settings) -> QdrantVectorIndex:
    try:
        return QdrantVectorIndex(
            qdrant_url=settings.qdrant_url,
            collection_name=settings.qdrant_collection,
        )
    except RuntimeError as exc:
        if str(exc) != "qdrant_client_not_installed":
            raise
        return QdrantVectorIndex(
            qdrant_url=settings.qdrant_url,
            collection_name=settings.qdrant_collection,
            client=_UnavailableQdrantClient(),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    sqlite_store = SQLiteStore(resolved_settings.sqlite_path)
    object_store = LocalObjectStore(resolved_settings.storage_root)
    parser = SimpleTextParser()
    splitter = SimpleTextSplitter()
    embedding_provider = OllamaEmbeddingProvider(
        base_url=resolved_settings.embedding_base_url,
        model=resolved_settings.embedding_model,
    )
    vector_index = _build_vector_index(resolved_settings)

    document_service = DocumentService(
        document_repository=sqlite_store,
        object_store=object_store,
        vector_index=vector_index,
        max_upload_bytes=resolved_settings.max_upload_bytes,
        allowed_extensions=resolved_settings.allowed_file_extensions,
    )
    ingestion_service = IngestionService(
        document_repository=sqlite_store,
        chunk_repository=sqlite_store,
        index_job_repository=sqlite_store,
        document_parser=parser,
        text_splitter=splitter,
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
    retrieval_service = RetrievalService(
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
    health_service = HealthService(
        sqlite=sqlite_store,
        qdrant=vector_index,
        ollama=embedding_provider,
    )
    mcp_server = None
    mcp_app = None
    if resolved_settings.mcp_enabled:
        mcp_server, mcp_app = create_mcp_app(retrieval_service, resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_server is None:
            yield
        else:
            async with mcp_server.session_manager.run():
                yield

    fastapi_app = FastAPI(title="N-KB", lifespan=lifespan)
    fastapi_app.state.settings = resolved_settings
    fastapi_app.state.services = {
        "documents": document_service,
        "ingestion": ingestion_service,
        "index_jobs": sqlite_store,
        "retrieval": retrieval_service,
        "health": health_service,
    }
    register_routes(fastapi_app)
    if mcp_app is not None:
        fastapi_app.mount(resolved_settings.mcp_path, mcp_app, name="mcp")
    static_root = Path(__file__).resolve().parent / "interfaces" / "http" / "static"
    fastapi_app.mount("/static", StaticFiles(directory=static_root), name="static")
    return fastapi_app


app = create_app()
