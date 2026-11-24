from fastapi import APIRouter, Request

from app.interfaces.http.errors import run_with_error_mapping
from app.interfaces.http.schemas import (
    RetrievalSearchRequest,
    RetrievalSearchResponse,
    context_to_response,
    request_filters_to_domain,
    retrieval_result_to_response,
)

router = APIRouter()


def retrieval_service(request: Request):
    return request.app.state.services["retrieval"]


@router.post("/retrieval/search", response_model=RetrievalSearchResponse)
def search(request: Request, body: RetrievalSearchRequest):
    """检索搜索；命中文档缺失或非 indexed 时结果可能少于 top_k。"""
    def action():
        filters = request_filters_to_domain(body.filters, body.min_score)
        outcome = retrieval_service(request).search_with_context(
            body.query, filters=filters, top_k=body.top_k
        )
        return RetrievalSearchResponse(
            query=body.query,
            results=[retrieval_result_to_response(result) for result in outcome.results],
            evaluation_context=context_to_response(outcome.context),
        )

    return run_with_error_mapping(action, runtime_code="infrastructure_error")
