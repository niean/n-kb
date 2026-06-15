from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.composition import build_services
from app.config import Settings
from app.interfaces.http.routes import register_routes
from app.interfaces.mcp.server import create_mcp_app


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    services = build_services(resolved_settings)
    mcp_server = None
    mcp_app = None
    mcp_status_app = None
    if resolved_settings.mcp_enabled:
        mcp_server, mcp_app, mcp_status_app = create_mcp_app(
            services.retrieval,
            services.health,
            resolved_settings,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_server is None:
            yield
        else:
            async with mcp_server.session_manager.run():
                yield

    fastapi_app = FastAPI(title="N-KB", lifespan=lifespan)
    fastapi_app.state.settings = resolved_settings
    fastapi_app.state.services = {
        "documents": services.documents,
        "ingestion": services.ingestion,
        "index_jobs": services.index_jobs,
        "retrieval": services.retrieval,
        "health": services.health,
    }
    register_routes(fastapi_app)
    if mcp_app is not None and mcp_status_app is not None:
        fastapi_app.add_api_route(
            f"{resolved_settings.mcp_path}/status",
            mcp_status_app,
            methods=["GET"],
            name="mcp-status",
        )
        fastapi_app.mount(resolved_settings.mcp_path, mcp_app, name="mcp")
    static_root = Path(__file__).resolve().parent / "interfaces" / "http" / "static"
    fastapi_app.mount("/static", StaticFiles(directory=static_root), name="static")
    return fastapi_app


app = create_app()
