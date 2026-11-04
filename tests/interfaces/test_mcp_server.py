import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.interfaces.mcp.server import create_mcp_server, noop_suppress_scope


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
    # T7：services 由 lifespan 填充，注册表键在启动后保持不变。
    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    with TestClient(app):
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

    monkeypatch.setattr(mcp_stdio, "build_services", lambda settings, **kwargs: Services())

    server = mcp_stdio.create_stdio_server(Settings(_env_file=None))
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["status"]


@pytest.mark.asyncio
async def test_stdio_server_accepts_injected_services(monkeypatch):
    from app import mcp_stdio

    class Services:
        health = FakeHealthService()

    services = Services()
    monkeypatch.setattr(
        mcp_stdio,
        "build_services",
        lambda settings, **kwargs: pytest.fail("services were injected"),
    )

    server = mcp_stdio.create_stdio_server(Settings(_env_file=None), services=services)
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["status"]


def test_stdio_main_runs_stdio_transport(monkeypatch):
    from app import mcp_stdio

    events = []

    class Server:
        def run(self, transport="stdio"):
            events.append(("run", transport))

    server = Server()

    class Services:
        health = FakeHealthService()

    services = Services()
    monkeypatch.setattr(
        mcp_stdio, "Settings", lambda: Settings(_env_file=None, otel_enabled=False)
    )
    monkeypatch.setattr(
        mcp_stdio, "build_services", lambda settings, **kwargs: services
    )
    monkeypatch.setattr(
        mcp_stdio,
        "create_stdio_server",
        lambda settings=None, services=None: events.append(
            ("create", settings, services)
        )
        or server,
    )

    mcp_stdio.main()

    assert len(events) == 2
    create_event, run_event = events
    assert create_event[0] == "create"
    assert create_event[1].otel_enabled is False  # main 只读取一次 Settings
    assert create_event[2] is services
    assert run_event == ("run", "stdio")


def test_stdio_main_releases_observability_when_server_construction_fails(monkeypatch):
    from app import mcp_stdio

    released = []
    handle = type("H", (), {"release": staticmethod(lambda: released.append(True))})()

    class Services:
        health = FakeHealthService()

    monkeypatch.setattr(
        mcp_stdio, "Settings", lambda: Settings(_env_file=None, otel_enabled=False)
    )
    monkeypatch.setattr(
        mcp_stdio, "build_services", lambda settings, **kwargs: Services()
    )
    monkeypatch.setattr(
        mcp_stdio,
        "start_observability",
        lambda settings: handle,
    )
    monkeypatch.setattr(
        mcp_stdio,
        "create_stdio_server",
        lambda settings=None, services=None: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        mcp_stdio.main()

    assert released == [True]


def test_stdio_module_entrypoint_exports_main():
    from app.interfaces.mcp import stdio

    assert stdio.main is not None


@pytest.mark.asyncio
async def test_stdio_channel_is_bound_at_tool_call_and_restored(monkeypatch):
    from app import mcp_stdio
    from app.application import telemetry_semantics as semantics
    snapshots = []
    previous_callback = semantics._completion_callback
    previous_scope = semantics._completion_scope_factory
    semantics.configure_completion(snapshots.append)
    services = type('Services', (), {'health': FakeHealthService()})()
    try:
        server = mcp_stdio.create_stdio_server(Settings(_env_file=None), services=services,
                                               suppress_scope=noop_suppress_scope)
        assert 'nkb.request.channel' not in semantics.current_operation_context()
        await server.call_tool('status', {})
        assert snapshots[-1].event == 'nkb.mcp.completed'
        assert snapshots[-1].attributes['nkb.request.channel'] == 'mcp_stdio'
        assert snapshots[-1].attributes['nkb.mcp.operation'] == 'status'
        assert not any(key.startswith('nkb.http.') for key in snapshots[-1].attributes)
        assert 'nkb.request.channel' not in semantics.current_operation_context()
    finally:
        semantics.configure_completion(previous_callback, scope_factory=previous_scope)
