from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from fastapi import Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse

from app.application.health_service import HealthService
from app.application import telemetry_semantics
from app.config import Settings

# T7：组合根注入的 suppress scope 工厂（抑制依赖健康调用的 CLIENT span）。
# Interfaces 不 import Infrastructure/SDK：默认为本模块提供的 no-op scope。
SuppressScopeFactory = Callable[[], Any]


@contextmanager
def noop_suppress_scope():
    """默认安全的 no-op suppress scope（未注入组合根工厂时使用）。"""
    yield


def mcp_status_payload(health_service: HealthService) -> dict[str, Any]:
    return {
        "status": health_service.process_health()["status"],
        "components": health_service.dependency_health(),
    }


def create_mcp_status_endpoint(suppress_scope: SuppressScopeFactory | None = None):
    """HTTP /mcp/status 端点：依赖健康调用包在注入的 suppress scope 内。

    health 服务在请求时从 app.state.services 取得（lifespan 启动后填充）。
    """
    scope_factory = suppress_scope or noop_suppress_scope

    async def mcp_status_endpoint(request: Request):
        health_service = request.app.state.services.get("health")
        if health_service is None:
            # lifespan 未启动（启动中/启动失败）：与 mounted MCP 的 503 一致。
            return PlainTextResponse("N-KB MCP is not ready", status_code=503)
        with scope_factory():
            payload = mcp_status_payload(health_service)
        return JSONResponse(payload)

    return mcp_status_endpoint


def create_mcp_server(
    health_service: HealthService,
    settings: Settings,
    *,
    suppress_scope: SuppressScopeFactory | None = None,
) -> FastMCP:
    scope_factory = suppress_scope or noop_suppress_scope
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
            with telemetry_semantics.operation_span("nkb.mcp.status"), scope_factory():
                return mcp_status_payload(health_service)
        except Exception:
            raise RuntimeError("infrastructure_error") from None

    return mcp


def create_mcp_app(
    health_service: HealthService,
    settings: Settings,
    *,
    suppress_scope: SuppressScopeFactory | None = None,
) -> tuple[FastMCP, Any]:
    """构造 MCP server 与其 Streamable HTTP ASGI app（status 端点由组合根注册）。"""
    server = create_mcp_server(health_service, settings, suppress_scope=suppress_scope)

    return (
        server,
        server.streamable_http_app(),
    )
