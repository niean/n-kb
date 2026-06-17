from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.application.health_service import HealthService
from app.config import Settings


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
    health_service: HealthService,
    settings: Settings,
):
    server = create_mcp_server(health_service, settings)

    return (
        server,
        server.streamable_http_app(),
        create_mcp_status_endpoint(health_service),
    )
