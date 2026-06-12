from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


KNOWN_VALUE_ERROR_CODES = {
    "unsupported_file_type",
    "file_too_large",
    "invalid_tags",
    "document_not_found",
    "index_job_not_found",
    "embedding_vector_mismatch",
}

VALIDATION_STATUS_CODES = {
    "document_not_found": 404,
    "index_job_not_found": 404,
    "unsupported_file_type": 400,
    "file_too_large": 400,
    "invalid_tags": 400,
    "embedding_vector_mismatch": 400,
    "validation_error": 400,
}


def stable_error_payload(code: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": code}}


def raise_stable_error(code: str, status_code: int) -> None:
    raise HTTPException(status_code=status_code, detail=stable_error_payload(code))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def stable_http_exception_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "http_error", "message": "http_error"}})


def normalize_value_error_code(exc: ValueError) -> str:
    code = str(exc) or exc.__class__.__name__
    if code == "invalid tag":
        code = "invalid_tags"
    if code not in KNOWN_VALUE_ERROR_CODES:
        return "validation_error"
    return code


def run_with_error_mapping(action: Callable[[], Any], runtime_code: str = "infrastructure_error") -> Any:
    try:
        return action()
    except HTTPException:
        raise
    except ValueError as exc:
        code = normalize_value_error_code(exc)
        raise_stable_error(code, VALIDATION_STATUS_CODES.get(code, 400))
    except Exception:
        raise_stable_error(runtime_code, 502)
