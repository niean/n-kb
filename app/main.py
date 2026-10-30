from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse

from app.application import telemetry_semantics
from app.composition import (
    build_observation_snapshot,
    build_services,
    start_observability,
)
from app.config import Settings
from app.infrastructure.observability.instrumentors import suppress_client_spans
from app.interfaces.http.routes import register_routes
from app.interfaces.mcp.server import create_mcp_app, create_mcp_status_endpoint

SERVICE_KEYS = ("documents", "ingestion", "index_jobs", "retrieval", "health")

_MCP_STATE_ATTR = "mcp_asgi_app"


class _LifespanMountedApp:
    """启动时委托：请求时从 app.state 解析真实 mounted ASGI app。

    MCP 服务在 lifespan 中构造（观测/全局埋点先于业务 client），而 mount
    必须在 create_app 时注册（不得启动后追加路由或重建 middleware stack）。
    本委托以固定 state 属性名转发；真实 app 未就绪（未启动/启动中/启动
    失败）时返回 503，不重建任何对象。
    """

    def __init__(self, fastapi_app: FastAPI, state_attr: str = _MCP_STATE_ATTR):
        self._fastapi_app = fastapi_app
        self._state_attr = state_attr

    async def __call__(self, scope, receive, send) -> None:
        scope_type = scope.get("type")
        if scope_type == "websocket":
            # MCP Streamable HTTP 是纯 HTTP 传输：WebSocket 一律礼貌关闭。
            try:
                await send({"type": "websocket.close", "code": 1013})
            except Exception:
                pass
            return
        if scope_type != "http":
            return
        target = getattr(self._fastapi_app.state, self._state_attr, None)
        if target is None:
            await PlainTextResponse(
                "N-KB MCP is not ready", status_code=503
            )(scope, receive, send)
            return
        await target(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        # 1. 观测先于一切业务资源；禁用返回 None（零 SDK import）。
        handle = start_observability(resolved_settings)
        # T9：观测句柄就绪后立即替换为真实快照 callable（None=disabled
        # 合同；冲突/降级句柄透传 degraded，不误报 disabled）。
        fastapi_app.state.observation_snapshot = build_observation_snapshot(
            resolved_settings, handle
        )
        session_cm = None
        try:
            # 2. global instrumentors/logging 已在 start_observability 中就绪，
            #    此刻才创建业务 client/connection（build_services）。
            services = build_services(
                resolved_settings,
                embedding_observer=telemetry_semantics.embedding_observer,
            )
            fastapi_app.state.services = {
                key: getattr(services, key) for key in SERVICE_KEYS
            }
            fastapi_app.state.observability_handle = handle
            # 3. MCP 服务构造 + session manager（T8 的快照 callable 注入点）。
            if resolved_settings.mcp_enabled:
                mcp_server, mcp_asgi_app = create_mcp_app(
                    services.health,
                    resolved_settings,
                    suppress_scope=suppress_client_spans,
                )
                setattr(fastapi_app.state, _MCP_STATE_ATTR, mcp_asgi_app)
                # 进入成功后才登记 session_cm：进入失败不得触发对未进入
                # CM 的 __aexit__（否则 RuntimeError 掩盖原始启动错误）。
                pending_cm = mcp_server.session_manager.run()
                await pending_cm.__aenter__()
                session_cm = pending_cm
            # 4. 服务请求。
            yield
        finally:
            # 先结束业务/session，再 release（观测运行时最后关闭）。
            # release 必须无条件执行（即使 session 关闭出错），且其自身
            # 故障不掩盖 session 关闭错误。
            try:
                if session_cm is not None:
                    await session_cm.__aexit__(None, None, None)
            finally:
                if handle is not None:
                    try:
                        handle.release()
                    except Exception:
                        try:
                            sys.stderr.write(
                                "n-kb: observability handle release failed "
                                "during lifespan shutdown\n"
                            )
                            sys.stderr.flush()
                        except Exception:
                            pass

    fastapi_app = FastAPI(title="N-KB", lifespan=lifespan)
    fastapi_app.state.settings = resolved_settings
    # services 由 lifespan 填充；构造期保持空注册表（不提前创建业务资源）。
    fastapi_app.state.services = {}
    fastapi_app.state.observability_handle = None
    # T9：构造期注入 disabled 合同的纯函数快照（lifespan 中替换为真实句柄）。
    fastapi_app.state.observation_snapshot = build_observation_snapshot(
        resolved_settings, None
    )
    fastapi_app.state.mcp_asgi_app = None
    register_routes(fastapi_app)
    if resolved_settings.mcp_enabled:
        fastapi_app.add_api_route(
            f"{resolved_settings.mcp_path}/status",
            create_mcp_status_endpoint(suppress_client_spans),
            methods=["GET"],
            name="mcp-status",
        )
        fastapi_app.mount(
            resolved_settings.mcp_path,
            _LifespanMountedApp(fastapi_app),
            name="mcp",
        )
    static_root = Path(__file__).resolve().parent / "interfaces" / "http" / "static"
    fastapi_app.mount("/static", StaticFiles(directory=static_root), name="static")
    if resolved_settings.otel_enabled:
        # T5 合同：middleware stack 尚未构建，create_app 返回前安装埋点。
        from app.infrastructure.observability.instrumentors import instrument_app

        instrument_app(fastapi_app, settings=resolved_settings)
    return fastapi_app


app = create_app()
