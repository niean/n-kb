from fastapi import APIRouter, Request

from app.interfaces.http.errors import raise_stable_error, run_with_error_mapping
from app.interfaces.http.schemas import IndexJobResponse, job_to_response

router = APIRouter()


def ingestion_service(request: Request):
    return request.app.state.services["ingestion"]


def index_job_reader(request: Request):
    services = request.app.state.services
    return services.get("index_jobs") or services["ingestion"]


@router.post("/documents/{document_id}/index", response_model=IndexJobResponse)
def index_document(request: Request, document_id: str):
    return run_with_error_mapping(
        lambda: job_to_response(ingestion_service(request).index_document(document_id)),
        runtime_code="indexing_failed",
    )


@router.get("/index-jobs/{job_id}", response_model=IndexJobResponse)
def get_index_job(request: Request, job_id: str):
    def action():
        job = index_job_reader(request).get_job(job_id)
        if job is None:
            raise_stable_error("index_job_not_found", 404)
        return job_to_response(job)

    return run_with_error_mapping(action)
