import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.interfaces.mcp.server import create_mcp_server


def structured_tool_result(result):
    return result[1]


class FakeHealthService:
    def process_health(self):
        return {"status": "ok"}

    def dependency_health(self):
        return {
            "sqlite": {"status": "ok"},
            "qdrant": {"status": "ok"},
            "ollama": {"status": "error"},
        }




@pytest.mark.asyncio
async def test_status_tool_uses_health_service():
    server = create_mcp_server(FakeHealthService(), Settings(_env_file=None))

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
async def test_mcp_server_only_exposes_status_tool():
    server = create_mcp_server(FakeHealthService(), Settings(_env_file=None))

    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["status"]
    assert tools[0].description == "查询 N-KB 服务进程和 SQLite、Qdrant、Ollama 等依赖组件的健康状态。"


def mounted_paths(app):
    return [route.path for route in app.routes if isinstance(route, Mount)]


def test_create_app_service_registry_remains_available():
    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    assert set(app.state.services) == {"documents", "ingestion", "index_jobs", "retrieval", "health"}



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


@pytest.mark.asyncio
async def test_stdio_server_exposes_existing_mcp_tools(monkeypatch):
    from app import mcp_stdio

    class Services:
        health = FakeHealthService()

    monkeypatch.setattr(mcp_stdio, "build_services", lambda settings: Services())

    server = mcp_stdio.create_stdio_server(Settings(_env_file=None))
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["status"]


def test_stdio_main_runs_stdio_transport(monkeypatch):
    from app import mcp_stdio

    class Server:
        call = None

        def run(self, transport="stdio"):
            self.call = transport

    server = Server()
    monkeypatch.setattr(mcp_stdio, "create_stdio_server", lambda: server)

    mcp_stdio.main()

    assert server.call == "stdio"


def test_stdio_module_entrypoint_exports_main():
    from app.interfaces.mcp import stdio

    assert stdio.main is not None
