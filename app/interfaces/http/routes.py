from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.interfaces.http import documents, health, indexing, retrieval
from app.interfaces.http.errors import register_error_handlers


def register_routes(app: FastAPI) -> None:
    register_error_handlers(app)

    @app.get("/", include_in_schema=False)
    def redirect_to_management_fe() -> RedirectResponse:
        return RedirectResponse(url="/static/index.html")
    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(indexing.router)
    app.include_router(retrieval.router)
