from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.application.health_service import HealthService
from app.application.retrieval_service import RetrievalService
from app.config import Settings
from app.domain.retrieval import RetrievalFilter, RetrievalResult


def retrieval_result_to_public_dict(result: RetrievalResult) -> dict[str, Any]:
    return {
        "document_id": result.document_id,
        "chunk_id": result.chunk_id,
        "score": result.score,
        "snippet": result.snippet,
        "source": result.source,
        "tags": result.tags,
        "metadata": {key: value for key, value in result.metadata.items() if key != "vector"},
    }


def mcp_status_payload(health_service: HealthService) -> dict[str, Any]:
    return {
        "status": health_service.process_health()["status"],
        "components": health_service.dependency_health(),
    }


def create_mcp_status_endpoint(health_service: HealthService):
    async def status_endpoint():
        return JSONResponse(mcp_status_payload(health_service))

    return status_endpoint


def create_mcp_server(
    retrieval_service: RetrievalService,
    health_service: HealthService,
    settings: Settings,
) -> FastMCP:
    mcp = FastMCP(
        settings.mcp_name,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )

    @mcp.tool(
        description="检索 N-KB 知识库，按查询文本、标签、来源类型和文档状态返回相关知识片段。",
        structured_output=True,
    )
    def search_knowledge(
        query: str,
        top_k: int = 5,
        min_score: float = 0.5,
        tags: dict[str, str] | None = None,
        source_kind: str | None = None,
        document_status: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(query) <= 2000:
            raise ValueError("invalid_query")
        if not 1 <= top_k <= 50:
            raise ValueError("invalid_top_k")
        if not 0 <= min_score <= 1:
            raise ValueError("invalid_min_score")

        filters = RetrievalFilter(
            tags=tags or {},
            source_kind=source_kind,
            document_status=document_status,
            min_score=min_score,
        )
        try:
            results = retrieval_service.search(query, filters=filters, top_k=top_k)
        except Exception:
            raise RuntimeError("infrastructure_error") from None
        return {
            "query": query,
            "results": [retrieval_result_to_public_dict(result) for result in results],
        }

    @mcp.tool(
        description="查询 N-KB 服务进程和 SQLite、Qdrant、Ollama 等依赖组件的健康状态。",
        structured_output=True,
    )
    def status() -> dict[str, Any]:
        try:
            return mcp_status_payload(health_service)
        except Exception:
            raise RuntimeError("infrastructure_error") from None

    return mcp


def create_mcp_app(
    retrieval_service: RetrievalService,
    health_service: HealthService,
    settings: Settings,
):
    server = create_mcp_server(retrieval_service, health_service, settings)

    return (
        server,
        server.streamable_http_app(),
        create_mcp_status_endpoint(health_service),
    )
