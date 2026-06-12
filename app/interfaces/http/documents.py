from fastapi import APIRouter, File, Form, Request, UploadFile

from app.application.document_service import UploadDocumentCommand
from app.domain.document import DocumentStatus
from app.domain.tag import parse_tags
from app.interfaces.http.errors import raise_stable_error, run_with_error_mapping
from app.interfaces.http.schemas import (
    ChunkResponse,
    DocumentContentResponse,
    DocumentResponse,
    chunk_to_response,
    content_to_response,
    document_to_response,
)

router = APIRouter()

UPLOAD_CHUNK_SIZE = 64 * 1024


def document_service(request: Request):
    return request.app.state.services["documents"]


def ingestion_service(request: Request):
    return request.app.state.services["ingestion"]


def parse_tag_filter(value: str | None) -> dict[str, str] | None:
    tags = parse_tags(value)
    if not tags:
        return None
    return {tag.key: tag.value for tag in tags}


def max_upload_bytes(request: Request, service) -> int | None:
    service_limit = getattr(service, "max_upload_bytes", None)
    if service_limit is not None:
        return int(service_limit)
    settings = getattr(request.app.state, "settings", None)
    settings_limit = getattr(settings, "max_upload_bytes", None)
    if settings_limit is not None:
        return int(settings_limit)
    return None


async def read_upload_content(request: Request, file: UploadFile, service) -> bytes:
    limit = max_upload_bytes(request, service)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if limit is not None and total > limit:
            raise_stable_error("file_too_large", 400)
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", response_model=DocumentResponse)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    source: str | None = Form(None),
    tags: str | None = Form(None),
):
    service = document_service(request)
    content = await read_upload_content(request, file, service)

    def action():
        document = service.upload_document(
            UploadDocumentCommand(
                filename=file.filename or "upload.txt",
                content=content,
                source=source,
                tags=tags,
            )
        )
        indexed_job = ingestion_service(request).index_document(document.id)
        indexed_document = service.get_document(indexed_job.document_id) or document
        return document_to_response(indexed_document, service.get_tags(indexed_job.document_id))

    return run_with_error_mapping(action)


@router.get("/documents", response_model=list[DocumentResponse])
def list_documents(request: Request, tags: str | None = None, status: DocumentStatus | None = None):
    def action():
        tag_filter = parse_tag_filter(tags)
        documents = document_service(request).list_documents(tags=tag_filter, status=status)
        return [document_to_response(document) for document in documents]

    return run_with_error_mapping(action)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(request: Request, document_id: str):
    def action():
        service = document_service(request)
        document = service.get_document(document_id)
        if document is None:
            raise_stable_error("document_not_found", 404)
        return document_to_response(document, service.get_tags(document_id))

    return run_with_error_mapping(action)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(request: Request, document_id: str):
    def action():
        document_service(request).delete_document(document_id)

    return run_with_error_mapping(action)


@router.get("/documents/{document_id}/content", response_model=DocumentContentResponse)
def get_document_content(request: Request, document_id: str):
    def action():
        content = document_service(request).get_content(document_id)
        if content is None:
            raise_stable_error("document_not_found", 404)
        return content_to_response(content)

    return run_with_error_mapping(action)


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkResponse])
def get_document_chunks(request: Request, document_id: str):
    def action():
        return [chunk_to_response(chunk) for chunk in document_service(request).list_chunks(document_id)]

    return run_with_error_mapping(action)
