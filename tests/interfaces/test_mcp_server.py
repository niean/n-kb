from dataclasses import asdict

import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from app.config import Settings
from app.domain.retrieval import RetrievalResult
from app.main import create_app
from app.interfaces.mcp.server import create_mcp_server, retrieval_result_to_public_dict


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


class FakeHealthService:
    def process_health(self):
        return {"status": "ok"}

    def dependency_health(self):
        return {
            "sqlite": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "ollama": {"status": "error"},
        }



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
    server = create_mcp_server(service, FakeHealthService(), Settings(_env_file=None))

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
    server = create_mcp_server(FakeRetrievalService(), FakeHealthService(), Settings(_env_file=None))

    with pytest.raises(ToolError):
        await server.call_tool("search_knowledge", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("qdrant host secret"), OSError("ollama host secret")])
async def test_search_knowledge_tool_masks_runtime_errors(error):
    class FailingRetrievalService(FakeRetrievalService):
        def search(self, query, filters=None, top_k=5):
            raise error

    server = create_mcp_server(FailingRetrievalService(), FakeHealthService(), Settings(_env_file=None))

    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("search_knowledge", {"query": "rag"})

    assert "infrastructure_error" in str(exc_info.value)
    assert "qdrant host secret" not in str(exc_info.value)
    assert "ollama host secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_status_tool_uses_health_service():
    server = create_mcp_server(FakeRetrievalService(), FakeHealthService(), Settings(_env_file=None))

    result = await server.call_tool("status", {})

    assert structured_tool_result(result) == {
        "status": "ok",
        "components": {
            "sqlite": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "ollama": {"status": "error"},
        },
    }


@pytest.mark.asyncio
async def test_mcp_server_only_exposes_search_and_status_tools():
    server = create_mcp_server(FakeRetrievalService(), FakeHealthService(), Settings(_env_file=None))

    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["search_knowledge", "status"]
    assert tools[0].description == "检索 N-KB 知识库，按查询文本、标签、来源类型和文档状态返回相关知识片段。"
    assert tools[1].description == "查询 N-KB 服务进程和 SQLite、Qdrant、Ollama 等依赖组件的健康状态。"


def mounted_paths(app):
    return [route.path for route in app.routes if isinstance(route, Mount)]


def test_create_app_does_not_mount_mcp_when_disabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    assert "/mcp" not in mounted_paths(app)


def test_create_app_exposes_mcp_and_status_sites_when_enabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))
    route_paths = [getattr(route, "path", None) for route in app.routes]

    assert "/mcp" in mounted_paths(app)
    assert "/mcp/status" in route_paths
    assert any(getattr(route, "path", None) == "/retrieval/search" for route in app.routes)


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:80", "http://127.0.0.1:80"),
        ("localhost:8212", "http://localhost:8212"),
        ("nkb.localhost:8212", "http://nkb.localhost:8212"),
        ("n-kb:8212", "http://n-kb:8212"),
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


def test_mcp_streamable_http_rejects_unlisted_origin():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": "127.0.0.1:80",
                "Origin": "http://example.com",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 403
    assert response.text == "Invalid Origin header"


def test_mcp_streamable_http_rejects_invalid_content_type():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "text/plain",
                "Host": "127.0.0.1:80",
                "Origin": "http://127.0.0.1:80",
            },
            content="{}",
        )

    assert response.status_code == 400
    assert response.text == "Invalid Content-Type header"


def test_mcp_status_endpoint_is_available_when_enabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=True, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.get(
            "/mcp/status",
            headers={"Host": "127.0.0.1:80", "Origin": "http://127.0.0.1:80"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert set(response.json()["components"]) == {"sqlite", "qdrant", "ollama"}


def test_mcp_status_endpoint_is_not_available_when_disabled():
    app = create_app(Settings(_env_file=None, mcp_enabled=False, mcp_path="/mcp"))

    with TestClient(app) as client:
        response = client.get("/mcp/status")

    assert response.status_code == 404
