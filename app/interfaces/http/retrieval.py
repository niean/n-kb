from fastapi import APIRouter, Request

from app.interfaces.http.errors import run_with_error_mapping
from app.interfaces.http.schemas import (
    RetrievalSearchRequest,
    RetrievalSearchResponse,
    request_filters_to_domain,
    retrieval_result_to_response,
)

router = APIRouter()


def retrieval_service(request: Request):
    return request.app.state.services["retrieval"]


@router.post("/retrieval/search", response_model=RetrievalSearchResponse)
def search(request: Request, body: RetrievalSearchRequest):
    def action():
        filters = request_filters_to_domain(body.filters)
        results = retrieval_service(request).search(body.query, filters=filters, top_k=body.top_k)
        if body.min_score is not None:
            results = [result for result in results if result.score >= body.min_score]
        return RetrievalSearchResponse(
            query=body.query,
            results=[retrieval_result_to_response(result) for result in results],
        )

    return run_with_error_mapping(action, runtime_code="infrastructure_error")
