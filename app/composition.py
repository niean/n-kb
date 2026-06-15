from dataclasses import dataclass

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


class _UnavailableQdrantClient:
    def get_collections(self):
        raise RuntimeError("qdrant_client_not_installed")


@dataclass(frozen=True)
class AppServices:
    documents: DocumentService
    ingestion: IngestionService
    index_jobs: SQLiteStore
    retrieval: RetrievalService
    health: HealthService


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


def build_services(settings: Settings) -> AppServices:
    sqlite_store = SQLiteStore(settings.sqlite_path)
    object_store = LocalObjectStore(settings.storage_root)
    parser = SimpleTextParser()
    splitter = SimpleTextSplitter()
    embedding_provider = OllamaEmbeddingProvider(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
    )
    vector_index = _build_vector_index(settings)

    document_service = DocumentService(
        document_repository=sqlite_store,
        object_store=object_store,
        vector_index=vector_index,
        max_upload_bytes=settings.max_upload_bytes,
        allowed_extensions=settings.allowed_file_extensions,
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
    return AppServices(
        documents=document_service,
        ingestion=ingestion_service,
        index_jobs=sqlite_store,
        retrieval=retrieval_service,
        health=health_service,
    )
