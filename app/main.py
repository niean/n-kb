from contextlib import asynccontextmanager, suppress
from pathlib import Path
import asyncio
import logging
import sqlite3
import sys
import threading

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

# 外部清理恢复调度周期（启动期先同步调用一次，此后周期兜底）；
# 常量模块级固定，不做配置面
_EVALUATION_CLEANUP_RETRY_INTERVAL_SECONDS = 60.0

_MCP_STATE_ATTR = "mcp_asgi_app"

logger = logging.getLogger(__name__)


def _log_worker_death(task: asyncio.Task) -> None:
    """投递循环死亡必须可见：_claim_due 对非 busy 存储故障快速失败终止
    run()，应用继续服务但投递已停。只记异常类型名，不含正文/凭据。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("evaluation delivery worker died type=%s", type(exc).__name__)


def _warmup_embedding(provider) -> None:
    """启动期后台预热：一次最小 embed 请求触发 Ollama 模型加载，把冷加载
    窗口从首个真实请求挪到应用启动期。失败静默（Ollama 未就绪时留给首个
    真实请求，读超时已覆盖冷加载窗口）。"""
    try:
        provider.embed_query("n-kb embedding warmup")
    except Exception:
        pass


async def _cleanup_retry_loop(document_service) -> None:
    """评估删除的外部清理周期兜底调度。

    启动期已在 lifespan 内同步执行一次恢复；本循环只做周期兜底（外部资源
    清理最终一致，失败留待下次），随取消退出，不作为业务异常出口。
    """
    while True:
        await asyncio.sleep(_EVALUATION_CLEANUP_RETRY_INTERVAL_SECONDS)
        try:
            document_service.retry_pending_cleanups()
        except Exception:
            # 单轮失败不终止调度（与恢复入口的逐行 busy 自愈口径一致）
            logger.warning("evaluation cleanup retry sweep failed")


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
        cleanup_task = None
        services = None
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
            # 3. 评估采集：启动期恢复一次（扫描 cleanup_pending=1 控制行重试
            #    外部清理，remote_acked 且清理完成才删控制行），再登记周期
            #    兜底调度；恢复逻辑在 lifespan 而非删除路径。读侧 busy 突发
            #    不中止应用启动，交给周期兜底调度下次再试。
            try:
                services.documents.retry_pending_cleanups()
            except sqlite3.OperationalError:
                logger.warning("evaluation startup recovery deferred (store busy)")
            cleanup_task = asyncio.create_task(
                _cleanup_retry_loop(services.documents)
            )
            # embedding 模型预热（后台线程，不阻塞启动；keep_alive 默认 -1
            # 常驻后仅启动期加载一次）。
            if services.embedding_provider is not None:
                threading.Thread(
                    target=_warmup_embedding,
                    args=(services.embedding_provider,),
                    name="nkb-embedding-warmup",
                    daemon=True,
                ).start()
            # endpoint 或 token 缺失时不启动网络投递循环（本地捕获/清理照常）。
            if services.delivery_worker is not None:
                worker_task = asyncio.create_task(services.delivery_worker.run())
                worker_task.add_done_callback(_log_worker_death)
            # 4. MCP 服务构造 + session manager（T8 的快照 callable 注入点）。
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
            # 5. 服务请求。
            yield
        finally:
            # 先停评估投递：worker.close 最多等在途请求 15s（停止领取后取消
            # 任务，未确认行保留），再回收组合根持有的投递客户端；随后停恢复
            # 调度，再结束业务/session，最后 release（观测运行时最后关闭）。
            # 各段关闭互不掩盖，release/session 必须无条件执行。
            try:
                if services is not None and services.delivery_worker is not None:
                    await services.delivery_worker.close(15)
                    if services.delivery_client is not None:
                        await services.delivery_client.aclose()
            finally:
                if cleanup_task is not None:
                    cleanup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await cleanup_task
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
