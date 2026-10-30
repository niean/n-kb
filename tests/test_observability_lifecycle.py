"""T7: 组合根、embedding 回调与生命周期测试。

验证 plan-261029-otel-observability T7 各条目：

- create_app 仅构造 app/路由/mount/middleware；build_services 与 MCP 构造
  移入 lifespan；app.state.services 维持原字典键（lifespan 填充）。
- 模块级 ``import app.main`` 不启动观测线程；构造异常、lifespan 启动失败
  与从未启动的 app 无泄漏。
- 禁用（N_KB_OTEL_ENABLED=false）隔离进程运行 HTTP/MCP/FE 与 stdio：
  sys.modules 无 SDK/instrumentation/exporter、无 py-spy/观测线程、stdout
  无观测输出。父测试进程不预先 import SDK（断言全部在子进程内做）。
- 启用：重复 app 引用计数共享、最后引用释放、配置冲突隔离（独立 degraded
  诊断句柄）、lifespan 启动失败释放运行时。
- MCP status suppress scope 由组合根注入（含 no-op 默认），依赖健康调用在
  suppress 下不产生依赖 CLIENT span（SERVER 父级存在时）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 禁用断言的模块黑名单前缀（不含 opentelemetry API 本身）。
_BANNED_MODULE_PREFIXES = (
    "opentelemetry.sdk",
    "opentelemetry.exporter",
    "opentelemetry.instrumentation",
)
_BANNED_THREAD_MARKERS = (
    "n-kb-profiling-sampler",
    "n-kb-observability-shutdown",
    "opentelemetry",
    "py-spy",
)


def _run_isolated(script: str, tmp_path: Path, extra_env: dict | None = None) -> list[str]:
    """在全新解释器中运行 script（父进程不预先 import SDK），返回失败行。"""
    extra_env = dict(extra_env or {})
    env = {
        **os.environ,
        "N_KB_OTEL_ENABLED": extra_env.pop("N_KB_OTEL_ENABLED", "false"),
        "N_KB_SQLITE_PATH": str(tmp_path / "n-kb.db"),
        "N_KB_STORAGE_ROOT": str(tmp_path / "data"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"isolated process failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    failures = [line for line in proc.stdout.splitlines() if line.startswith("FAIL:")]
    return failures


# ---------------------------------------------------------------------------
# HTTP 生命周期（进程内）
# ---------------------------------------------------------------------------


def test_create_app_defers_services_to_lifespan():
    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    # 构造期不建立业务服务，也不启动观测。
    assert getattr(app.state, "services", {}) == {}

    with TestClient(app) as client:
        assert set(app.state.services) == {
            "documents",
            "ingestion",
            "index_jobs",
            "retrieval",
            "health",
        }
        assert client.get("/health").json() == {"status": "ok"}


def test_create_app_signature_unchanged():
    # 位置参数与默认 None 均保持。
    app = create_app(Settings(_env_file=None, mcp_enabled=False))
    assert app.state.settings.mcp_enabled is False
    app_default = create_app()
    assert app_default.state.settings is not None


# ---------------------------------------------------------------------------
# 禁用（零 SDK）隔离进程
# ---------------------------------------------------------------------------

_DISABLED_HTTP_SCRIPT = """
import sys, threading

import app.main  # 模块级 app 构造：不得启动观测
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

banned_prefixes = ("opentelemetry.sdk", "opentelemetry.exporter",
                   "opentelemetry.instrumentation")
banned_threads = ("n-kb-profiling-sampler", "n-kb-observability-shutdown",
                  "opentelemetry", "py-spy")

app = create_app(Settings(_env_file=None, mcp_enabled=True))
with TestClient(app) as client:
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/mcp/status", headers={
        "Host": "127.0.0.1:80", "Origin": "http://127.0.0.1:80"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert client.get("/static/index.html").status_code == 200
    assert client.get("/documents").status_code == 200

violations = [m for m in sys.modules if m.startswith(banned_prefixes)]
if violations:
    print("FAIL: sdk modules imported:", sorted(violations))
names = [t.name for t in threading.enumerate()]
bad = [n for n in names if any(marker in n for marker in banned_threads)]
if bad:
    print("FAIL: observability threads running:", bad)
print("OK")
"""


def test_disabled_http_mcp_fe_startup_imports_no_observability_sdk(tmp_path):
    failures = _run_isolated(_DISABLED_HTTP_SCRIPT, tmp_path)
    assert failures == []


_DISABLED_STDIO_SCRIPT = """
import asyncio, contextlib, io, sys

from app.config import Settings
from app import mcp_stdio

banned_prefixes = ("opentelemetry.sdk", "opentelemetry.exporter",
                   "opentelemetry.instrumentation")

stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    server = mcp_stdio.create_stdio_server(Settings(_env_file=None))
    tools = asyncio.run(server.list_tools())

if [tool.name for tool in tools] != ["status"]:
    print("FAIL: unexpected tools", tools)
if stdout.getvalue():
    print("FAIL: stdout is not pure MCP protocol:", repr(stdout.getvalue()[:200]))
violations = [m for m in sys.modules if m.startswith(banned_prefixes)]
if violations:
    print("FAIL: sdk modules imported:", sorted(violations))
print("OK")
"""


def test_disabled_stdio_setup_writes_nothing_to_stdout(tmp_path):
    failures = _run_isolated(_DISABLED_STDIO_SCRIPT, tmp_path)
    assert failures == []


# ---------------------------------------------------------------------------
# 启用：引用计数共享 / 冲突隔离 / 失败释放（隔离进程，真实 SDK）
# ---------------------------------------------------------------------------

_ENABLED_SHARED_SCRIPT = """
import sys

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

base = dict(
    _env_file=None,
    otel_enabled=True,
    otel_exporter_otlp_endpoint="http://127.0.0.1:9",  # 不可达端点：仅装配
    profiling_enabled=False,
)

app1 = create_app(Settings(**base))
app2 = create_app(Settings(**base))
with TestClient(app1) as client1:
    handle1 = app1.state.observability_handle
    if handle1 is None:
        print("FAIL: app1 has no observability handle")
    with TestClient(app2) as client2:
        handle2 = app2.state.observability_handle
        if handle2 is None:
            print("FAIL: app2 has no observability handle")
        elif handle1 is not None and handle1.instance_id != handle2.instance_id:
            print("FAIL: same-config apps did not share the runtime")
        if client1.get("/health").status_code != 200:
            print("FAIL: app1 request failed after app2 startup")
        if client2.get("/health").status_code != 200:
            print("FAIL: app2 request failed")
        # 配置冲突：独立 degraded 诊断句柄，不影响原运行时。
        conflict_config = {**base, "otel_exporter_otlp_endpoint": "http://127.0.0.1:1"}
        app3 = create_app(Settings(**conflict_config))
        with TestClient(app3) as client3:
            handle3 = app3.state.observability_handle
            if handle3 is None:
                print("FAIL: conflict app has no diagnostic handle")
            else:
                snapshot3 = handle3.snapshot()
                if snapshot3["diagnostic_error"] != "config_conflict":
                    print("FAIL: conflict not isolated:", snapshot3["diagnostic_error"])
                snapshot = handle3.snapshot()
                if snapshot["status"] != "degraded":
                    print("FAIL: conflict handle not degraded:", snapshot["status"])
            if handle1 is not None and handle1.instance_id != app1.state.observability_handle.instance_id:
                print("FAIL: original runtime disturbed by conflict app")
    # app2 退出后运行时仍被 app1 持有。
    from app.infrastructure.observability.setup import get_runtime_handle
    if handle1 is not None and get_runtime_handle() is None:
        print("FAIL: runtime released while app1 still active")
    if client1.get("/health").status_code != 200:
        print("FAIL: app1 request failed after app2 release")

from app.infrastructure.observability.setup import get_runtime_handle
if get_runtime_handle() is not None:
    print("FAIL: runtime not released after last app closed")
print("OK")
"""


def test_enabled_repeated_apps_share_runtime_and_release_on_last_close(tmp_path):
    failures = _run_isolated(
        _ENABLED_SHARED_SCRIPT, tmp_path, {"N_KB_OTEL_ENABLED": "true"}
    )
    assert failures == []


_ENABLED_STARTUP_FAILURE_SCRIPT = """
import sys

import app.main as app_main
from app.config import Settings
from fastapi.testclient import TestClient

def _boom(settings, **kwargs):
    raise RuntimeError("boom")

app_main.build_services = _boom

from app.main import create_app

app = create_app(Settings(_env_file=None, otel_enabled=True,
                          otel_exporter_otlp_endpoint="http://127.0.0.1:9",
                          profiling_enabled=False))
try:
    with TestClient(app):
        pass
    print("FAIL: lifespan startup failure did not propagate")
except RuntimeError as exc:
    if "boom" not in str(exc):
        print("FAIL: unexpected error:", exc)
except Exception as exc:  # noqa: BLE001
    print("FAIL: unexpected error type:", type(exc).__name__, exc)

from app.infrastructure.observability.setup import get_runtime_handle
if get_runtime_handle() is not None:
    print("FAIL: runtime leaked after lifespan startup failure")
print("OK")
"""


def test_enabled_lifespan_startup_failure_releases_runtime(tmp_path):
    failures = _run_isolated(
        _ENABLED_STARTUP_FAILURE_SCRIPT,
        tmp_path,
        {"N_KB_OTEL_ENABLED": "true"},
    )
    assert failures == []


_ENABLED_MODULE_IMPORT_SCRIPT = """
import sys, threading

import app.main  # 模块级 app：启用配置下也只是构造

from app.infrastructure.observability.setup import get_runtime_handle

if get_runtime_handle() is not None:
    print("FAIL: module-level app import assembled the runtime")
banned_threads = ("n-kb-profiling-sampler", "n-kb-observability-shutdown")
names = [t.name for t in threading.enumerate()]
bad = [n for n in names if any(marker in n for marker in banned_threads)]
if bad:
    print("FAIL: observability threads running after import:", bad)
print("OK")
"""


def test_enabled_module_level_app_import_does_not_assemble_runtime(tmp_path):
    failures = _run_isolated(
        _ENABLED_MODULE_IMPORT_SCRIPT, tmp_path, {"N_KB_OTEL_ENABLED": "true"}
    )
    assert failures == []


_ENABLED_STDIO_SCRIPT = """
import sys

from app import mcp_stdio
from app.config import Settings

class Server:
    def __init__(self):
        self.ran = False
    def run(self, transport="stdio"):
        self.ran = True

server = Server()
services_holder = {}
real_build = mcp_stdio.build_services

def fake_build(settings, **kwargs):
    services_holder["built"] = True
    services_holder["observer"] = kwargs.get("embedding_observer")
    return type("S", (), {"health": object()})()

mcp_stdio.build_services = fake_build
mcp_stdio.create_stdio_server = lambda settings=None, services=None: (
    services_holder.__setitem__("services", services) or server)

mcp_stdio.main()

if not server.ran:
    print("FAIL: server.run was not called")
if not services_holder.get("built"):
    print("FAIL: build_services was not called")
if services_holder.get("observer") is None:
    print("FAIL: embedding observer factory was not passed")
if services_holder.get("services") is None:
    print("FAIL: services were not passed to create_stdio_server")

from app.infrastructure.observability.setup import get_runtime_handle
if get_runtime_handle() is not None:
    print("FAIL: runtime not released after stdio main")
print("OK")
"""


def test_enabled_stdio_main_builds_services_once_and_releases(tmp_path):
    failures = _run_isolated(
        _ENABLED_STDIO_SCRIPT, tmp_path, {"N_KB_OTEL_ENABLED": "true"}
    )
    assert failures == []


# ---------------------------------------------------------------------------
# MCP status suppress 注入（机制级：SERVER 父级 + suppress 不产生依赖 CLIENT）
# ---------------------------------------------------------------------------


class _ListSpanExporter:
    def __init__(self, spans):
        self._spans = spans
        self._lock = threading.Lock()

    def export(self, spans):
        with self._lock:
            self._spans.extend(spans)
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


@pytest.fixture()
def spy_tracer():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    spans = []
    exporter = _ListSpanExporter(spans)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, spans
    provider.shutdown()


def test_suppressed_dependency_call_under_server_parent_creates_no_client(spy_tracer):
    from opentelemetry import trace as trace_api
    from opentelemetry.trace import SpanKind

    from app.infrastructure.observability.instrumentors import (
        _ProxyTracerProvider,
        suppress_client_spans,
    )

    provider, spans = spy_tracer
    filtering_tracer = _ProxyTracerProvider(provider, allowlist=None).get_tracer("test")

    server_span = filtering_tracer.start_span("POST /mcp", kind=SpanKind.SERVER)
    with trace_api.use_span(server_span, end_on_exit=True):
        # 无 suppress：SERVER 父级有效，CLIENT span 正常创建（对照组）。
        client_span = filtering_tracer.start_span(
            "GET dependency", kind=SpanKind.CLIENT
        )
        assert client_span.get_span_context().is_valid
        client_span.end()
        # suppress scope 内：同一 SERVER 父级下不再产生依赖 CLIENT。
        with suppress_client_spans():
            suppressed = filtering_tracer.start_span(
                "GET dependency", kind=SpanKind.CLIENT
            )
        assert not suppressed.get_span_context().is_valid

    client_spans = [span for span in spans if span.kind == SpanKind.CLIENT]
    assert len(client_spans) == 1  # 仅对照组产生一个 CLIENT span


class _SpySuppressScope:
    def __init__(self):
        self.events = []

    @contextmanager
    def __call__(self):
        self.events.append("enter")
        try:
            yield
        finally:
            self.events.append("exit")


@pytest.mark.asyncio
async def test_mcp_status_tool_wraps_dependency_health_in_injected_scope():
    from app.interfaces.mcp.server import create_mcp_server

    class FakeHealth:
        def process_health(self):
            return {"status": "ok"}

        def dependency_health(self):
            return {"sqlite": {"status": "ok"}}

    spy = _SpySuppressScope()
    server = create_mcp_server(
        FakeHealth(), Settings(_env_file=None), suppress_scope=spy
    )

    result = await server.call_tool("status", {})

    assert result[1]["status"] == "ok"
    assert spy.events == ["enter", "exit"]


def test_mcp_status_endpoint_wraps_dependency_health_in_injected_scope():
    from fastapi import FastAPI

    from app.interfaces.mcp.server import create_mcp_status_endpoint

    class FakeHealth:
        def process_health(self):
            return {"status": "ok"}

        def dependency_health(self):
            return {"sqlite": {"status": "ok"}}

    spy = _SpySuppressScope()
    app = FastAPI()
    app.state.services = {"health": FakeHealth()}
    app.add_api_route(
        "/mcp/status", create_mcp_status_endpoint(spy), methods=["GET"]
    )

    with TestClient(app) as client:
        response = client.get("/mcp/status")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert spy.events == ["enter", "exit"]


def test_mcp_status_default_suppress_scope_is_safe_noop():
    from fastapi import FastAPI

    from app.interfaces.mcp.server import create_mcp_status_endpoint

    class FakeHealth:
        def process_health(self):
            return {"status": "ok"}

        def dependency_health(self):
            return {"sqlite": {"status": "ok"}}

    app = FastAPI()
    app.state.services = {"health": FakeHealth()}
    app.add_api_route("/mcp/status", create_mcp_status_endpoint(), methods=["GET"])

    with TestClient(app) as client:
        response = client.get("/mcp/status")

    assert response.status_code == 200
    assert response.json()["components"] == {"sqlite": {"status": "ok"}}


# ---------------------------------------------------------------------------
# 评审修正：lifespan finally 错误路径
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self):
        self.released = 0
        self.release_error: Exception | None = None

    @property
    def instance_id(self):
        return "fake-instance"

    def release(self):
        self.released += 1
        if self.release_error is not None:
            raise self.release_error


class _SessionCM:
    def __init__(self, enter_error=None, exit_error=None):
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        if self.exit_error is not None:
            raise self.exit_error
        return None


class _FakeSessionManager:
    def __init__(self, cm):
        self._cm = cm

    def run(self):
        return self._cm


class _FakeServer:
    def __init__(self, cm):
        self.session_manager = _FakeSessionManager(cm)


def _patch_mcp_lifespan(monkeypatch, handle, server):
    from app import main as app_main

    monkeypatch.setattr(app_main, "start_observability", lambda settings: handle)
    monkeypatch.setattr(
        app_main,
        "create_mcp_app",
        lambda health, settings, suppress_scope=None: (server, object()),
    )


def test_lifespan_session_enter_failure_releases_handle_and_surfaces_error(
    monkeypatch,
):
    import app.main as app_main

    handle = _FakeHandle()
    cm = _SessionCM(enter_error=RuntimeError("session boom"))
    _patch_mcp_lifespan(monkeypatch, handle, _FakeServer(cm))

    app = app_main.create_app(Settings(_env_file=None, mcp_enabled=True))

    with pytest.raises(RuntimeError, match="session boom"):
        with TestClient(app):
            pass

    assert cm.entered == 1
    assert cm.exited == 0  # 未进入成功：绝不调用 __aexit__
    assert handle.released == 1


def test_lifespan_session_exit_failure_still_releases_handle(monkeypatch):
    import app.main as app_main

    handle = _FakeHandle()
    cm = _SessionCM(exit_error=RuntimeError("exit boom"))
    _patch_mcp_lifespan(monkeypatch, handle, _FakeServer(cm))

    app = app_main.create_app(Settings(_env_file=None, mcp_enabled=True))

    with pytest.raises(RuntimeError, match="exit boom"):
        with TestClient(app):
            pass

    assert cm.exited == 1
    assert handle.released == 1


def test_lifespan_release_failure_does_not_mask_session_exit_error(monkeypatch):
    import app.main as app_main

    handle = _FakeHandle()
    handle.release_error = RuntimeError("release boom")
    cm = _SessionCM(exit_error=RuntimeError("exit boom"))
    _patch_mcp_lifespan(monkeypatch, handle, _FakeServer(cm))

    app = app_main.create_app(Settings(_env_file=None, mcp_enabled=True))

    with pytest.raises(RuntimeError, match="exit boom"):
        with TestClient(app):
            pass

    assert handle.released == 1


def test_mcp_endpoints_return_503_before_lifespan_startup():
    # 就绪一致性：未启动时 /mcp/status 与 mounted MCP 都是干净的 503。
    app = create_app(Settings(_env_file=None, mcp_enabled=True))
    client = TestClient(app)  # 不进入 context：lifespan 不运行

    status = client.get("/mcp/status")
    mount = client.post(
        "/mcp/",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    assert status.status_code == 503
    assert mount.status_code == 503


def test_mcp_websocket_scope_is_closed_gracefully():
    import asyncio

    from fastapi.routing import Mount

    app = create_app(Settings(_env_file=None, mcp_enabled=True))
    mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp")

    sent = []

    async def receive():
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/mcp/", "headers": []}

    # 不抛异常，礼貌关闭（1013），而非服务器 500/崩溃路径。
    asyncio.run(mount.app(scope, receive, send))

    assert sent == [{"type": "websocket.close", "code": 1013}]


def test_disabled_start_preserves_active_details_callbacks(monkeypatch):
    from app import composition
    from app.infrastructure.observability import sanitizer
    def configure(*args):
        raise AssertionError('disabled start configured callbacks')
    previous = sanitizer._callbacks
    monkeypatch.setattr(sanitizer,'_callbacks',(configure,None,None,None))
    assert composition.start_observability(Settings(otel_enabled=False)) is None
    assert sanitizer._callbacks[0] is configure
    monkeypatch.setattr(sanitizer,'_callbacks',previous)


def test_shared_and_conflicting_start_do_not_replace_active_policy(monkeypatch):
    from app import composition
    from app.infrastructure.observability import setup, logging as obs, sanitizer
    from types import SimpleNamespace
    shared = SimpleNamespace(is_published=True, _diagnostic_error=None)
    diagnostic = SimpleNamespace(is_published=False, _diagnostic_error='config_conflict')
    monkeypatch.setattr(setup,'get_runtime_handle',lambda:shared)
    monkeypatch.setattr(setup,'setup_observability',lambda settings,**kwargs:
        shared if settings.otel_content_max_bytes == 2048 else diagnostic)
    def forbidden(*args,**kwargs):
        raise AssertionError('active callbacks replaced')
    monkeypatch.setattr(sanitizer,'bind_details',forbidden)
    monkeypatch.setattr(obs,'configure_execution_context',forbidden)
    assert composition.start_observability(Settings(otel_enabled=True,otel_content_max_bytes=2048)) is shared
    assert composition.start_observability(Settings(otel_enabled=True,otel_content_max_bytes=1024)) is diagnostic


def test_real_local_two_app_policy_snapshot_and_final_callback_cleanup(tmp_path):
    script = '''
import os, logging
from app.config import Settings
from app.composition import start_observability
from app.application import telemetry_details
from app.infrastructure.observability import logging as obs, sanitizer
os.environ['N_KB_TEST_API_KEY'] = 'first-catalog-sentinel'
settings = Settings(otel_enabled=True,otel_exporter_otlp_endpoint='',profiling_enabled=False)
first = start_observability(settings)
os.environ['N_KB_TEST_API_KEY'] = 'second-catalog-sentinel'
second = start_observability(settings)
conflict = start_observability(Settings(otel_enabled=True,otel_exporter_otlp_endpoint='',profiling_enabled=False,otel_content_max_bytes=1024))
if first is not second or conflict is first: print('FAIL: runtime ownership')
if sanitizer._pending_config[1] != 2048: print('FAIL: conflicting policy replaced active')
if first.release() != 1: print('FAIL: premature final release')
record = logging.LogRecord('app.test',logging.INFO,'',1,'first-catalog-sentinel',(),None)
if 'first-catalog-sentinel' in (obs.project_record(record).message or ''): print('FAIL: catalog changed')
if not telemetry_details.project_details(retrieval_input='readable'): print('FAIL: active policy removed')
second.release()
if sanitizer._callbacks is not None: print('FAIL: callbacks retained after final release')
if obs._detail_callbacks.get('project') is not None: print('FAIL: log callbacks retained')
'''
    assert _run_isolated(script,tmp_path) == []
