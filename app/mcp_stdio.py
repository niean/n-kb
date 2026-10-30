from typing import Any, Optional

from app.composition import build_services, get_suppress_client_spans, start_observability
from app.config import Settings
from app.interfaces.mcp.server import create_mcp_server


def create_stdio_server(
    settings: Settings | None = None,
    services: Any = None,
    suppress_scope: Any = None,
) -> Any:
    """构造 stdio MCP server。

    ``services`` 缺省时由本函数 build_services（旧调用能力保持）；组合根
    main 传入统一构建的 services，避免重复构建或丢失 embedding observer。
    ``suppress_scope`` 缺省时经组合根解析 Infrastructure 实现，本模块不直接
    import Infrastructure。
    """
    resolved_settings = settings or Settings()
    if services is None:
        from app.application import telemetry_semantics

        services = build_services(
            resolved_settings,
            embedding_observer=telemetry_semantics.embedding_observer,
        )
    if suppress_scope is None:
        suppress_scope = get_suppress_client_spans()
    return create_mcp_server(
        services.health,
        resolved_settings,
        suppress_scope=suppress_scope,
    )


def main() -> None:
    # 只读取一次 Settings；观测/global 注册在 build_services 之前（禁用也
    # 统一构建业务服务）。stdout 仅 MCP 协议：观测输出只走 stderr。
    settings = Settings()
    handle = start_observability(settings)
    try:
        from app.application import telemetry_semantics

        services = build_services(
            settings,
            embedding_observer=telemetry_semantics.embedding_observer,
        )
        server = create_stdio_server(settings, services=services)
        server.run(transport="stdio")
    finally:
        if handle is not None:
            handle.release()
