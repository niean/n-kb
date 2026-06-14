from dataclasses import asdict

import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from app.config import Settings
from app.domain.retrieval import RetrievalResult
from app.main import create_app
from app.interfaces.mcp.server import create_mcp_server, host_matches, origin_matches, retrieval_result_to_public_dict


def structured_tool_result(result):
    return result[1]


class FakeRetrievalService:
    def __init__(self):
        self.call = None

    def search(self, query, filters=None, top_k=5):
        self.call = {"query": query, "filters": filters, "top_k": top_k}
        return [
            RetrievalResult(
                document_id="doc-1",
                chunk_id="chunk-1",
                score=0.91,
                snippet="safe snippet",
                source={"kind": "upload", "uri": "note.md"},
                tags={"topic": "rag"},
                metadata={"ordinal": 0, "vector": [1.0, 2.0]},
            )
        ]


def test_localhost_wildcard_matchers_support_subdomains():
    assert host_matches("nkb.localhost", "*.localhost")
    assert host_matches("nkb.localhost:80", "*.localhost:*")
    assert origin_matches("http://nkb.localhost", "http://*.localhost")
    assert origin_matches("http://nkb.localhost:80", "http://*.localhost:*")
    assert not host_matches("localhost", "*.localhost")
    assert not origin_matches("https://nkb.localhost", "http://*.localhost")


def test_retrieval_result_public_mapping_removes_vector():
    result = RetrievalResult(
        document_id="doc-1",
        chunk_id="chunk-1",
        score=0.91,
        snippet="safe snippet",
        source={"kind": "upload", "uri": "note.md"},
        tags={"topic": "rag"},
        metadata={"ordinal": 0, "vector": [1.0, 2.0]},
    )

    assert retrieval_result_to_public_dict(result) == {
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "score": 0.91,
        "snippet": "safe snippet",
        "source": {"kind": "upload", "uri": "note.md"},
        "tags": {"topic": "rag"},
        "metadata": {"ordinal": 0},
    }


@pytest.mark.asyncio
async def test_search_knowledge_tool_uses_retrieval_service():
    service = FakeRetrievalService()
    server = create_mcp_server(service, Settings(_env_file=None))

    result = await server.call_tool(
        "search_knowledge",
        {
            "query": "what is rag",
            "top_k": 3,
            "min_score": 0.5,
            "tags": {"topic": "rag"},
            "source_kind": "upload",
            "document_status": "indexed",
        },
    )

    assert service.call["query"] == "what is rag"
    assert service.call["top_k"] == 3
    assert asdict(service.call["filters"]) == {
        "tags": {"topic": "rag"},
        "source_kind": "upload",
        "document_status": "indexed",
        "min_score": 0.5,
    }
    assert structured_tool_result(result) == {
        "query": "what is rag",
        "results": [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "score": 0.91,
                "snippet": "safe snippet",
                "source": {"kind": "upload", "uri": "note.md"},
                "tags": {"topic": "rag"},
                "metadata": {"ordinal": 0},
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "", "top_k": 5, "min_score": 0.5},
        {"query": "x", "top_k": 0, "min_score": 0.5},
        {"query": "x", "top_k": 51, "min_score": 0.5},
        {"query": "x", "top_k": 5, "min_score": -0.1},
        {"query": "x", "top_k": 5, "min_score": 1.1},
    ],
)
async def test_search_knowledge_tool_validates_input(arguments):
    server = create_mcp_server(FakeRetrievalService(), Settings(_env_file=None))

    with pytest.raises(ToolError):
        await server.call_tool("search_knowledge", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("qdrant host secret"), OSError("ollama host secret")])
async def test_search_knowledge_tool_masks_runtime_errors(error):
    class FailingRetrievalService(FakeRetrievalService):
        def search(self, query, filters=None, top_k=5):
            raise error

    server = create_mcp_server(FailingRetrievalService(), Settings(_env_file=None))

    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("search_knowledge", {"query": "rag"})

    assert "infrastructure_error" in str(exc_info.value)
    assert "qdrant host secret" not in str(exc_info.value)
    assert "ollama host secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mcp_server_only_exposes_search_tool():
    server = create_mcp_server(FakeRetrievalService(), Settings(_env_file=None))

    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["search_knowledge"]


def mounted_paths(app):
    return [route.path for route in app.routes if isinstance(route, Mount)]


def test_create_app_does_not_mount_mcp_when_disabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    assert "/mcp" not in mounted_paths(app)


def test_create_app_mounts_mcp_when_enabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    assert "/mcp" in mounted_paths(app)
    assert any(getattr(route, "path", None) == "/retrieval/search" for route in app.routes)


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:80", "http://127.0.0.1:80"),
        ("n-kb:8212", "http://n-kb:8212"),
        ("nkb.localhost", "http://nkb.localhost"),
        ("abc.localhost:8212", "http://abc.localhost:8212"),
    ],
)
def test_mcp_streamable_http_initialize_endpoint_is_available(host, origin):
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": host,
                "Origin": origin,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.1"},
                },
            },
        )

    assert response.status_code not in {404, 405}
    assert response.headers["content-type"].startswith(("application/json", "text/event-stream"))


def test_mcp_streamable_http_rejects_unlisted_host():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": "example.com",
                "Origin": "http://example.com",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 421
    assert response.text == "Invalid Host header"
