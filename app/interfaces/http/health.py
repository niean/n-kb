from fastapi import APIRouter, Request

from app.interfaces.http.errors import run_with_error_mapping

router = APIRouter()


def health_service(request: Request):
    return request.app.state.services["health"]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/dependencies")
def dependency_health(request: Request):
    return run_with_error_mapping(lambda: health_service(request).dependency_health())
