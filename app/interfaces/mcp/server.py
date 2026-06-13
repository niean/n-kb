from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

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


def host_matches(value: str, allowed: str) -> bool:
    if value == allowed:
        return True
    if allowed == "*.localhost":
        return value.endswith(".localhost") and value != "localhost"
    if allowed == "*.localhost:*":
        host, separator, _port = value.partition(":")
        return bool(separator) and host_matches(host, "*.localhost")
    if allowed.endswith(":*"):
        return value.startswith(f"{allowed[:-2]}:")
    return False


def origin_matches(value: str, allowed: str) -> bool:
    parsed_value = urlsplit(value)
    parsed_allowed = urlsplit(allowed)
    if not parsed_value.scheme or not parsed_value.netloc:
        return False
    if parsed_value.scheme != parsed_allowed.scheme:
        return False
    return host_matches(parsed_value.netloc, parsed_allowed.netloc)


class McpTransportSecurityMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: list[str], allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        if not any(host_matches(headers.get("host", ""), allowed) for allowed in self.allowed_hosts):
            await Response("Invalid Host header", status_code=421)(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin and not any(origin_matches(origin, allowed) for allowed in self.allowed_origins):
            await Response("Invalid Origin header", status_code=403)(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_mcp_server(retrieval_service: RetrievalService, settings: Settings) -> FastMCP:
    server = FastMCP(
        settings.mcp_name,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(structured_output=True)
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

    return server


def create_mcp_app(retrieval_service: RetrievalService, settings: Settings):
    server = create_mcp_server(retrieval_service, settings)
    return server, McpTransportSecurityMiddleware(
        server.streamable_http_app(),
        settings.mcp_allowed_hosts,
        settings.mcp_allowed_origins,
    )
