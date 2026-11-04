"""T5: 自动埋点（FastAPI/httpx/sqlite3）安全合同测试。

告警（测试隔离）：本模块通过模块级 fixture 注册进程级全局 httpx/sqlite3
instrumentor（该注册行为本身也是被测对象）。注册后整个 pytest 会话期间
保持安装——后续测试中的 httpx transport 调用与 sqlite3 cursor 执行都会产生
（自动过滤、无导出的）span。不要假设其它测试模块里 sqlite3.connect /
httpx transport 仍是未包装状态。

覆盖 plan-261029-otel-observability T5 各条目：

- instrument_app 在 middleware stack 建立前安装；重复调用幂等；全局
  httpx/sqlite3 注册恰好一次（install_global_instrumentors）。
- 属性白名单在采集前生效（写入阶段投影）：测试 spy（SimpleSpanProcessor
  出口）断言 SDK 接收到的 ReadableSpan 属性/事件，绝不出现 URL query、
  userinfo、原始 SQL、异常文本哨兵。
- SERVER 仅现有注册业务路由（方法+模板白名单，模板来自真实路由表）；
  排除 /、health、static、MCP status、自检；禁用 ASGI receive/send span；
  未知路径不使用原始 URL 命名；方法、query、尾斜杠均覆盖。
- W3C 传播：traceparent 远端父级 → 唯一 SERVER span，trace/span 继承。
- HTTPX/SQLite 仅有效父操作下创建 CLIENT；suppress_client_spans 显式抑制。
- HTTPX 完成生命周期：span 结束推迟到真实传输终态（流读完/close/异常/
  取消），http_client_completed Event 与 Log 各恰好一次。
- 入站 http_request_completed 每请求一次；未采样仍保留；管理写请求即使
  降噪也记录访问日志；无响应取消状态为空。
- 各出口秘密哨兵：SQL、URL、exc_info 不出现在 SDK 接收的 span 属性/事件。
"""
from __future__ import annotations

import asyncio
import http.server
import inspect
import json
import logging
import socketserver
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanKind, StatusCode

from app.infrastructure.observability import instrumentors as ins
from app.infrastructure.observability import logging as obs_logging

# 自动埋点属性白名单（模块公开合同）。
ALLOWED_ATTRS = ins.ALLOWED_AUTO_ATTRIBUTES | {"nkb.http.outcome", "nkb.http.summary", "nkb.http.direction", "nkb.request.operation", "nkb.request.channel"}

# 各出口秘密哨兵（SQL / URL / exc_info）。
SECRET_SQL = "SECRET-SQL-sentinel-T5"
SECRET_URL = "SECRET-URL-sentinel-T5"
SECRET_EXC = "SECRET-EXC-sentinel-T5"


class _ListSpanExporter:
    """线程安全收集已结束 ReadableSpan 的内存 exporter（SDK 接收值 spy）。"""

    def __init__(self, spans: list):
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


class _Settings:
    mcp_enabled = True
    mcp_path = "/mcp"
    otel_exporter_otlp_endpoint = "http://127.0.0.1:4317"
    profiling_pyroscope_endpoint = ""


class _McpApp:
    """MCP Streamable HTTP mount 的最小 ASGI 替身。"""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"jsonrpc":"2.0"}'})


class _StaticApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"static"})


def build_app() -> FastAPI:
    """按 N-KB 现有注册路由形状构建测试 app（模板来自真实路由表）。"""
    app = FastAPI()

    @app.post("/documents")
    async def upload_document():
        return {"id": "doc-1"}

    @app.get("/documents")
    def list_documents():
        return []

    @app.get("/documents/{document_id}")
    def get_document(document_id: str):
        return {"id": document_id}

    @app.delete("/documents/{document_id}", status_code=204)
    def delete_document(document_id: str):
        return None

    @app.get("/documents/{document_id}/content")
    def get_document_content(document_id: str):
        return {"content": "x"}

    @app.get("/documents/{document_id}/chunks")
    def get_document_chunks(document_id: str):
        return []

    @app.post("/documents/{document_id}/index")
    def index_document(document_id: str):
        if document_id == "boom":
            raise RuntimeError(SECRET_EXC)
        return {"job_id": "j-1", "state": "queued"}

    @app.get("/index-jobs/{job_id}")
    def get_index_job(job_id: str):
        return {"job_id": job_id, "state": "done"}

    @app.post("/retrieval/search")
    def search():
        return {"results": []}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/health/dependencies")
    def dependency_health():
        return {"status": "ok"}

    @app.get("/observations/otel")
    def observations_otel():
        return {"status": "healthy"}

    @app.get("/")
    def redirect_root():
        return {"root": True}

    async def mcp_status():
        return {"status": "ok"}

    app.add_api_route("/mcp/status", mcp_status, methods=["GET"], name="mcp-status")
    app.mount("/mcp", _McpApp())
    app.mount("/static", _StaticApp())
    return app


def _attrs(span) -> dict:
    return dict(span.attributes or {})


def _server_spans(spans):
    return [s for s in spans if s.kind == SpanKind.SERVER]


def _client_spans(spans):
    return [s for s in spans if s.kind == SpanKind.CLIENT]


@pytest.fixture(scope="module")
def instrumented():
    """一个已埋点的测试 app + 记录型 tracer provider + 全局注册。"""
    spans: list = []
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    app = build_app()
    settings = _Settings()
    first = ins.instrument_app(app, settings=settings, tracer_provider=provider)
    second = ins.instrument_app(app, settings=settings, tracer_provider=provider)
    globals_first = ins.install_global_instrumentors(
        settings=settings, tracer_provider=provider
    )
    # 捕获点在首次全局注册之后：断言第二次注册不重复包装 connect。
    sqlite_connect_after_first = sqlite3.connect
    globals_second = ins.install_global_instrumentors(
        settings=settings, tracer_provider=provider
    )
    return SimpleNamespace(
        spans=spans,
        app=app,
        provider=provider,
        settings=settings,
        first_call_installed=first,
        second_call_installed=second,
        globals_first=globals_first,
        globals_second=globals_second,
        sqlite_connect_after_first=sqlite_connect_after_first,
    )


@pytest.fixture(autouse=True)
def _clear_spans(instrumented, monkeypatch):
    from app.application import telemetry_contract, telemetry_requests
    from app.infrastructure.observability import snapshots
    monkeypatch.setattr(snapshots, "_execution_projector", telemetry_contract.project_execution_attributes)
    monkeypatch.setattr(ins, "_request_scope_factory", telemetry_requests.request_scope)
    monkeypatch.setattr(ins, "_request_snapshot", telemetry_requests.current_request_facts)
    instrumented.spans.clear()
    yield
    instrumented.spans.clear()


# ---------------------------------------------------------------------------
# 安装、顺序与幂等
# ---------------------------------------------------------------------------


def test_fastapi_installed_before_middleware_stack(instrumented):
    assert instrumented.first_call_installed is True
    assert getattr(instrumented.app, "_is_instrumented_by_opentelemetry", False)


def test_repeat_calls_idempotent(instrumented):
    assert instrumented.second_call_installed is True
    state = ins.global_registration_state()
    assert state["httpx"] is True
    assert state["sqlite3"] is True


def test_global_instrumentors_registered_exactly_once(instrumented):
    assert instrumented.globals_first is True
    assert instrumented.globals_second is True
    # sqlite3.connect 未被重复包装
    assert sqlite3.connect is instrumented.sqlite_connect_after_first
    # httpx transport 已被全局包装（0.66b1 包 HTTPTransport.handle_request）
    import httpx as _httpx

    assert hasattr(_httpx.HTTPTransport.handle_request, "__wrapped__")
    assert hasattr(_httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")


def test_install_after_middleware_stack_built_is_skipped(instrumented):
    app = FastAPI()
    app.middleware_stack = app.build_middleware_stack()
    installed = ins.instrument_app(
        app, settings=_Settings(), tracer_provider=instrumented.provider
    )
    assert installed is False
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)


def test_instrumentation_failure_never_propagates():
    class Broken:
        pass

    assert ins.instrument_app(Broken(), settings=_Settings()) is False


# ---------------------------------------------------------------------------
# SERVER 路由白名单（模板来自真实路由表）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected_name"),
    [
        ("POST", "/documents", "POST /documents"),
        ("GET", "/documents?tag=x&status=ok", "GET /documents"),
        ("GET", "/documents/doc-77", "GET /documents/{document_id}"),
        ("DELETE", "/documents/doc-77", "DELETE /documents/{document_id}"),
        ("GET", "/documents/doc-77/content", "GET /documents/{document_id}/content"),
        ("GET", "/documents/doc-77/chunks", "GET /documents/{document_id}/chunks"),
        ("POST", "/documents/doc-77/index", "POST /documents/{document_id}/index"),
        ("GET", "/index-jobs/job-9?wait=1", "GET /index-jobs/{job_id}"),
        ("POST", "/retrieval/search", "POST /retrieval/search"),
    ],
)
def test_server_span_uses_registered_template(instrumented, method, path, expected_name):
    with TestClient(instrumented.app) as client:
        response = client.request(method, path)
    assert response.status_code < 500
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1, [s.name for s in servers]
    assert servers[0].name == expected_name
    assert _attrs(servers[0]).get("http.route") == expected_name.split(" ", 1)[1]


def test_trailing_slash_redirect_yields_single_template_span(instrumented):
    with TestClient(instrumented.app) as client:
        response = client.get("/documents/doc-1/")
    assert response.status_code == 200
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1
    assert servers[0].name == "GET /documents/{document_id}"
    for span in instrumented.spans:
        assert "doc-1" not in span.name


def test_wrong_method_creates_no_server_span(instrumented):
    with TestClient(instrumented.app, raise_server_exceptions=False) as client:
        assert client.delete("/documents").status_code in (404, 405)
        assert client.post("/health").status_code in (404, 405)
        assert client.put("/retrieval/search").status_code in (404, 405)
    assert not _server_spans(instrumented.spans)


def test_unknown_path_creates_no_span_and_never_uses_raw_url(instrumented):
    with TestClient(instrumented.app) as client:
        assert client.get("/nonexistent-SECRET-99").status_code == 404
    assert not _server_spans(instrumented.spans)
    for span in instrumented.spans:
        assert "SECRET-99" not in span.name
        assert "SECRET-99" not in str(_attrs(span))


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/health",
        "/health/dependencies",
        "/health?probe=1",
        "/static/app.js",
        "/static/styles.css?v=2",
        "/observations/otel",
        "/observations/otel?refresh=1",
    ],
)
def test_noise_paths_excluded(instrumented, path):
    with TestClient(instrumented.app) as client:
        client.get(path)
    assert not _server_spans(instrumented.spans)


def test_mcp_status_excluded_but_mcp_subtree_traced(instrumented):
    with TestClient(instrumented.app) as client:
        assert client.get("/mcp/status").status_code == 200
        assert client.post("/mcp").status_code == 200
        assert client.post("/mcp/session-SECRET-1").status_code == 200
    servers = _server_spans(instrumented.spans)
    assert [s.name for s in servers] == ["POST /mcp", "POST /mcp"]
    for span in instrumented.spans:
        assert "SECRET-1" not in span.name


def test_no_asgi_receive_send_spans(instrumented):
    with TestClient(instrumented.app) as client:
        client.get("/documents")
    assert not any(
        "receive" in s.name or "send" in s.name for s in instrumented.spans
    )


def test_remote_parent_propagation_single_server_span(instrumented):
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    with TestClient(instrumented.app) as client:
        client.post("/retrieval/search", headers={"traceparent": traceparent})
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1
    span = servers[0]
    assert format(span.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert format(span.parent.span_id, "016x") == "00f067aa0ba902b7"


def test_unsampled_remote_parent_still_propagates_without_export(instrumented):
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
    with TestClient(instrumented.app) as client:
        client.post("/retrieval/search", headers={"traceparent": traceparent})
    assert not instrumented.spans


# ---------------------------------------------------------------------------
# 采集前白名单投影（SDK 接收值 spy）
# ---------------------------------------------------------------------------


def test_server_span_attributes_whitelisted(instrumented):
    with TestClient(instrumented.app) as client:
        client.get("/documents/doc-1?secret=" + SECRET_URL)
    servers = _server_spans(instrumented.spans)
    assert servers
    attrs = _attrs(servers[0])
    assert set(attrs) <= set(ALLOWED_ATTRS)
    assert attrs.get("http.route") == "/documents/{document_id}"
    for forbidden in ("http.url", "url.full", "http.target", "url.path", "url.query"):
        assert forbidden not in attrs
    assert SECRET_URL not in json.dumps(attrs, default=str)


def test_server_exception_events_and_status_description_blocked(instrumented):
    with TestClient(instrumented.app, raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError):
            client.post("/documents/boom/index")
    servers = _server_spans(instrumented.spans)
    assert servers
    span = servers[0]
    assert all(e.name == "nkb.http.completed" for e in span.events)
    if span.status is not None:
        assert not getattr(span.status, "description", None)
    assert SECRET_EXC not in json.dumps(
        {"attrs": dict(span.attributes or {}), "events": [dict(e.attributes or {}) for e in span.events]},
        default=str,
    )


def test_httpx_span_attributes_whitelisted_no_url_leak(instrumented):
    with instrumented.provider.get_tracer("test").start_as_current_span(
        "parent"
    ), httpx.Client(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.get(
                f"http://user:SECRET-PASS@127.0.0.1:1/leak?{SECRET_URL}#frag"
            )
    clients = _client_spans(instrumented.spans)
    assert clients
    for span in clients:
        attrs = _attrs(span)
        assert set(attrs) <= set(ALLOWED_ATTRS)
        for forbidden in ("http.url", "url.full", "url.query", "url.fragment"):
            assert forbidden not in attrs
        for value in attrs.values():
            assert "SECRET-PASS" not in str(value)
            assert SECRET_URL not in str(value)


def test_sqlite_span_no_statement_text(instrumented):
    conn = sqlite3.connect(":memory:")
    try:
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            conn.cursor().execute(
                f"SELECT '{SECRET_SQL}' AS secret_password FROM sqlite_master"
            ).fetchall()
        db_spans = [s for s in instrumented.spans if s.kind == SpanKind.CLIENT]
        assert db_spans
        for span in db_spans:
            attrs = _attrs(span)
            assert set(attrs) <= set(ALLOWED_ATTRS)
            for forbidden in ("db.statement", "db.query.text", "db.statement.parameters"):
                assert forbidden not in attrs
            assert SECRET_SQL not in span.name
            assert SECRET_SQL not in json.dumps(attrs, default=str)
            assert not any(event.name == "nkb.http.completed" for event in span.events)
            assert attrs.get("db.system") == "sqlite" or attrs.get(
                "db.system.name"
            )
    finally:
        conn.close()


def test_sqlite_table_projection_ignores_quoted_literals(instrumented):
    """回归：FROM/INTO 关键字出现在字符串字面量内时不得投影字面量内容。"""
    statement = "SELECT 'walked from harbor' AS note FROM documents"
    # 单元级：字面量剥离后再匹配
    assert ins._extract_table_name(statement) == "documents"
    # 字面量内容绝不作为表名投影（fail-closed）
    assert ins._extract_table_name("INSERT INTO 'docs' SELECT 1") != "docs"
    conn = sqlite3.connect(":memory:")
    try:
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            conn.cursor().execute(
                f"SELECT '{SECRET_SQL} walked from harbor' AS note FROM sqlite_master"
            ).fetchall()
        db_spans = [
            s for s in instrumented.spans
            if s.kind == SpanKind.CLIENT
            and "sqlite" in json.dumps(dict(s.attributes or {}), default=str)
        ]
        assert db_spans
        for span in db_spans:
            attrs = _attrs(span)
            assert attrs.get("db.collection.name") in (None, "sqlite_master")
            assert "harbor" not in json.dumps(attrs, default=str)
            assert "harbor" not in span.name
            assert SECRET_SQL not in json.dumps(attrs, default=str)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLIENT 门槛与抑制
# ---------------------------------------------------------------------------


def test_httpx_client_requires_valid_parent(instrumented):
    before = len(_client_spans(instrumented.spans))
    with httpx.Client(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.get("http://127.0.0.1:1/orphan")
    assert len(_client_spans(instrumented.spans)) == before


def test_sqlite_client_requires_valid_parent(instrumented):
    before = len(_client_spans(instrumented.spans))
    conn = sqlite3.connect(":memory:")
    try:
        conn.cursor().execute("SELECT 1").fetchall()
    finally:
        conn.close()
    assert len(_client_spans(instrumented.spans)) == before


def test_suppress_client_spans_blocks_httpx_and_sqlite(instrumented):
    before = len(_client_spans(instrumented.spans))
    conn = sqlite3.connect(":memory:")
    try:
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            with ins.suppress_client_spans():
                with httpx.Client(timeout=0.5) as client:
                    with pytest.raises(httpx.HTTPError):
                        client.get("http://127.0.0.1:1/suppressed")
                conn.cursor().execute(f"SELECT '{SECRET_SQL}'").fetchall()
        assert len(_client_spans(instrumented.spans)) == before
        # 抑制结束后恢复
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            conn.cursor().execute("SELECT 1").fetchall()
        assert len(_client_spans(instrumented.spans)) == before + 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 入站 http_request_completed
# ---------------------------------------------------------------------------


def _capture_http_logs(monkeypatch, event):
    logs = []

    class Capture(logging.Handler):
        def emit(self, record):
            if obs_logging.event_code_for(record) == event:
                logs.append(json.loads(obs_logging.SafeEventJsonFormatter().format(record)))

    logger = logging.getLogger(obs_logging.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    # isEnabledFor 结果按级别缓存；替换缓存字典避免跨测试串味。
    monkeypatch.setattr(logger, "_cache", {})
    handler = Capture()
    logger.addHandler(handler)
    return logs, handler, logger


@pytest.mark.parametrize("mode", ["normal", "stream", "error", "unsampled", "unsampled_error"])
def test_http_request_completed_matches_server_span(instrumented, mode, monkeypatch):
    from starlette.responses import StreamingResponse

    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_COMPLETED_EVENT
    )
    contexts = []
    try:
        app = FastAPI()

        @app.post("/retrieval/search")
        async def search():
            contexts.append(trace_api.get_current_span().get_span_context())
            if mode.endswith("error"):
                raise RuntimeError("secret-error-body")
            if mode == "stream":
                async def chunks():
                    yield b"first"
                    yield b"last"

                return StreamingResponse(chunks())
            return {"results": []}

        sampler = ALWAYS_OFF if mode.startswith("unsampled") else ALWAYS_ON
        provider = TracerProvider(sampler=sampler)
        provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter([])))
        assert ins.instrument_app(app, settings=_Settings(), tracer_provider=provider)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/retrieval/search?secret=hidden")
        assert response.status_code == (500 if mode.endswith("error") else 200)
        assert response.content == (b"firstlast" if mode == "stream" else response.content)
        assert len(logs) == 1, logs
        payload = logs[0]
        assert payload["trace_id"] == format(contexts[0].trace_id, "032x")
        assert payload["span_id"] == format(contexts[0].span_id, "016x")
        assert payload["http.request.method"] == "POST"
        assert payload["http.route"] == "/retrieval/search"
        if mode.endswith("error"):
            assert "http.response.status_code" not in payload  # outer error middleware sends later
        else:
            assert payload["http.response.status_code"] == response.status_code
        assert payload["nkb.http.duration_ms"] >= 0
        assert payload["nkb.http.outcome"] == (
            "failure" if mode.endswith("error") else "success"
        )
        assert "secret" not in json.dumps(payload)
        if mode.startswith("unsampled"):
            # 未采样：无导出 span，但完成日志保留并携带上下文 ID。
            assert not instrumented.spans
    finally:
        logger.removeHandler(handler)


def test_completion_event_exactly_once_with_bridge_attached(instrumented, monkeypatch):
    """回归：安全桥挂在事实 logger 上（生产装配）时完成事件不重复。

    _complete_* 显式补投 + SafeLogBridge._add_event_on_current_span
    重注入必须收敛为恰好一个完成事件。
    """
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())

    class _SpyOtelHandler(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    bridge = obs_logging.SafeLogBridge(otel_handler=_SpyOtelHandler())
    facts_logger = logging.getLogger(obs_logging.__name__)
    monkeypatch.setattr(facts_logger, "level", logging.INFO)
    monkeypatch.setattr(facts_logger, "_cache", {})
    facts_logger.addHandler(bridge)
    try:
        # SERVER 路径：走完整中间件完成链
        with TestClient(instrumented.app) as client:
            response = client.post("/retrieval/search")
        assert response.status_code == 200
        server_spans = [
            s for s in instrumented.spans
            if s.kind == SpanKind.SERVER and s.name == "POST /retrieval/search"
        ]
        assert server_spans
        events = [
            e for e in server_spans[0].events
            if e.name == obs_logging.HTTP_COMPLETED_EVENT
        ]
        assert len(events) == 1

        # CLIENT 路径：有效父级下的出站调用
        before = len(instrumented.spans)
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            with httpx.Client(timeout=0.5) as outbound:
                with pytest.raises(httpx.HTTPError):
                    outbound.get("http://127.0.0.1:1/once-only")
        client_spans = [
            s for s in instrumented.spans[before:]
            if s.kind == SpanKind.CLIENT
        ]
        assert client_spans
        client_events = [
            e for e in client_spans[0].events
            if e.name == obs_logging.HTTP_CLIENT_COMPLETED_EVENT
        ]
        assert len(client_events) == 1
    finally:
        facts_logger.removeHandler(bridge)


def test_http_cancelled_request_logs_once_without_status(monkeypatch):
    import asyncio

    calls = []
    monkeypatch.setattr(
        obs_logging,
        "log_http_request_completed",
        lambda *args, **kwargs: calls.append(args),
    )
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    tracer = ins._FilteringTracer(provider.get_tracer("cancel-test"))
    span = tracer.start_span(
        "POST /retrieval/search",
        kind=SpanKind.SERVER,
        attributes={"http.request.method": "POST", "http.route": "/retrieval/search"},
    )

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    async def unused(*args):
        pass

    try:
        with trace_api.use_span(span, end_on_exit=False):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(
                    ins._HttpAccessFactsMiddleware(cancelled)(
                        {"type": "http"}, unused, unused
                    )
                )
            span.end()
            span.end()
        assert len(calls) == 1
        assert calls[0][-1] == "cancelled"
        assert calls[0][2] is None  # 未发送响应前不伪造状态码
    finally:
        provider.shutdown()


def test_management_write_access_log_even_when_denoised(instrumented, monkeypatch):
    calls = []
    monkeypatch.setattr(
        obs_logging,
        "log_http_request_completed",
        lambda *args, **kwargs: calls.append(args),
    )
    with TestClient(instrumented.app, raise_server_exceptions=False) as client:
        client.post("/admin/SECRET-99")
        client.post("/health")  # 降噪路径的写请求不留访问日志
    assert not _server_spans(instrumented.spans)
    assert len(calls) == 1, calls
    method, route, status, duration, outcome = calls[0]
    assert method == "POST"
    assert route == "/admin/{redacted}"
    assert status == 404
    assert outcome == "failure"


def test_http_completion_log_failure_does_not_break_response_or_span(
    instrumented, monkeypatch
):
    def broken(*args):
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(obs_logging, "log_http_request_completed", broken)
    before = len(instrumented.spans)
    with TestClient(instrumented.app) as client:
        assert client.post("/retrieval/search").status_code == 200
    assert any(
        s.name == "POST /retrieval/search" for s in instrumented.spans[before:]
    )


# ---------------------------------------------------------------------------
# 出站 http_client_completed（真实传输终态）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("sampled", "status"), [(True, 200), (True, 503), (True, None), (False, 200)])
def test_outbound_request_event_and_log_once(instrumented, monkeypatch, sampled, status):
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    monkeypatch.setattr(
        ins,
        "_http_caller",
        lambda: "qdrant.request",
        raising=False,
    )
    try:
        if sampled:
            parent_provider = instrumented.provider
        else:
            # 未采样父级：上下文仍有效（ParentBased 继承），但无导出。
            parent_provider = TracerProvider(sampler=ALWAYS_OFF)
            parent_provider.add_span_processor(
                SimpleSpanProcessor(_ListSpanExporter([]))
            )
        parent_ctx = parent_provider.get_tracer("test")

        def respond(request):
            if status is None:
                raise httpx.ConnectError("sensitive " + SECRET_EXC, request=request)
            return httpx.Response(status)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor.instrument_client(
            client,
            tracer_provider=ins._FilteringTracerProvider(instrumented.provider),
            request_hook=ins._httpx_request_hook,
            response_hook=ins._httpx_response_hook,
        )
        try:
            with parent_ctx.start_as_current_span("parent"):
                if status is None:
                    with pytest.raises(httpx.HTTPError):
                        client.get(
                            f"https://user:SECRET-PASS@models.example/v1/models?{SECRET_URL}#frag"
                        )
                else:
                    assert (
                        client.get(
                            f"https://user:SECRET-PASS@models.example/v1/models?{SECRET_URL}"
                        ).status_code
                        == status
                    )
            assert len(logs) == 1, logs
            payload = logs[0]
            assert payload["server.address"] == "models.example"
            assert payload["url.template"] == "/v1/models"
            assert payload["nkb.http.caller"] == "qdrant.request"
            assert payload["nkb.http.outcome"] == ("failure" if status is None or status >= 400 else "success")
            assert payload["span_id"] != format(
                trace_api.get_current_span().get_span_context().span_id, "016x"
            )
            assert "SECRET-PASS" not in json.dumps(payload)
            assert SECRET_URL not in json.dumps(payload)
            assert SECRET_EXC not in json.dumps(payload)
            if sampled:
                span = next(
                    s
                    for s in instrumented.spans
                    if s.context.span_id == int(payload["span_id"], 16)
                )
                assert span.parent is not None
                events = [e for e in span.events if e.name == "nkb.http.completed"]
                assert len(events) == 1
                assert SECRET_URL not in json.dumps(
                    {"attrs": dict(span.attributes or {}), "events": [dict(e.attributes or {}) for e in span.events]},
                    default=str,
                )
            else:
                assert not instrumented.spans
        finally:
            client.close()
    finally:
        logger.removeHandler(handler)


def test_outbound_cancelled_request_outcome(monkeypatch):
    import asyncio

    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter([])))
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        async def respond(request):
            raise asyncio.CancelledError()

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        HTTPXClientInstrumentor.instrument_client(
            client,
            tracer_provider=ins._FilteringTracerProvider(provider),
            # P057 已核验：instrument_client 无 async_* hook 参数，
            # 异步 hook 由 iscoroutinefunction 自动识别。
            request_hook=ins._async_httpx_request_hook,
            response_hook=ins._async_httpx_response_hook,
        )

        async def run():
            with pytest.raises(asyncio.CancelledError):
                await client.get("https://api.example/private/SECRET-1")

        async def with_parent():
            with provider.get_tracer("test").start_as_current_span("parent"):
                await run()

        try:
            asyncio.run(with_parent())
            assert len(logs) == 1
            assert logs[0]["nkb.http.outcome"] == "cancelled"
            assert logs[0]["url.template"] == "/private/{redacted}"
        finally:
            pass
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def test_outbound_async_success_response_hook_and_completion(monkeypatch, instrumented):
    """回归：异步成功响应路径。

    0.66b1 httpx 以 (span, request_info, response_info) 三参调用异步
    response hook 且无 try/except 保护；签名错误会以 TypeError 中断
    业务调用。同时覆盖 http_client_completed 的耗时与状态出口。
    """
    import asyncio

    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    provider = instrumented.provider
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        async def respond(request):
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        HTTPXClientInstrumentor.instrument_client(
            client,
            tracer_provider=ins._FilteringTracerProvider(provider),
            # P057 已核验：instrument_client 无 async_* hook 参数，
            # 异步 hook 由 iscoroutinefunction 自动识别。
            request_hook=ins._async_httpx_request_hook,
            response_hook=ins._async_httpx_response_hook,
        )

        async def run():
            return await client.get("https://api.example/v1/models")

        async def with_parent():
            try:
                with provider.get_tracer("test").start_as_current_span("parent"):
                    return await run()
            finally:
                await client.aclose()

        response = asyncio.run(with_parent())
        # 业务调用未被 hook 签名中断
        assert response.status_code == 200
        assert len(logs) == 1, logs
        payload = logs[0]
        assert payload["nkb.http.outcome"] == "success"
        assert payload["http.response.status_code"] == 200
        assert payload["server.address"] == "api.example"
        assert payload["url.template"] == "/v1/models"
        assert payload["nkb.http.duration_ms"] >= 0
        client_span = next(
            s for s in instrumented.spans if s.kind == SpanKind.CLIENT
        )
        assert client_span.attributes.get("http.response.status_code") == 200
        events = [
            e for e in client_span.events
            if e.name == "nkb.http.completed"
        ]
        assert len(events) == 1
        assert events[0].attributes.get("nkb.http.duration_ms") >= 0
    finally:
        logger.removeHandler(handler)


def test_outbound_orphan_request_produces_nothing(monkeypatch):
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter([])))
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
        HTTPXClientInstrumentor.instrument_client(
            client,
            tracer_provider=ins._FilteringTracerProvider(provider),
            request_hook=ins._httpx_request_hook,
            response_hook=ins._httpx_response_hook,
        )
        try:
            assert client.get("https://api.example/orphan").status_code == 200
            assert logs == []
        finally:
            client.close()
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def test_sync_stream_defers_completion_until_read(instrumented, monkeypatch):
    """真实 HTTPTransport 流式响应：读完才结束 span 并记录一次完成日志。"""
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"0123456789"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            with httpx.Client(timeout=5.0) as client:
                request = client.build_request("GET", f"http://127.0.0.1:{port}/stream")
                response = client.send(request, stream=True)
                assert logs == [], "响应体未读完不得提前完成"
                assert response.read() == b"0123456789"
                assert len(logs) == 1, logs
                assert logs[0]["nkb.http.outcome"] == "success"
                assert logs[0]["url.template"] == "/stream"
    finally:
        server.shutdown()
        server.server_close()
        logger.removeHandler(handler)


def test_sync_stream_close_without_read_logs_incomplete(instrumented, monkeypatch):
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b"x" * 64)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with instrumented.provider.get_tracer("test").start_as_current_span("parent"):
            with httpx.Client(timeout=5.0) as client:
                request = client.build_request("GET", f"http://127.0.0.1:{port}/stream")
                response = client.send(request, stream=True)
                response.close()
                assert len(logs) == 1
                assert logs[0]["nkb.http.outcome"] == "unknown"
    finally:
        server.shutdown()
        server.server_close()
        logger.removeHandler(handler)


def test_stream_wrapper_unit_read_error_and_once(monkeypatch):
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter([])))
    try:
        tracer = ins._FilteringTracer(provider.get_tracer("unit"))
        with tracer.start_as_current_span("parent"):
            with tracer.start_as_current_span(
                "GET host /p", kind=SpanKind.CLIENT
            ) as span:
                lifecycle = ins._ClientLifecycle(span)
                span._lifecycle = lifecycle
                # 传输返回但流未读完：end 被推迟。
                span.end()
                assert logs == []

                class Underlying(httpx.SyncByteStream):
                    def __init__(self):
                        self.reads = 0

                    def __iter__(self):
                        self.reads += 1
                        yield b"a"
                        yield b"b"

                underlying = Underlying()
                stream = ins._SyncStream(underlying, lifecycle)
                assert list(stream) == [b"a", b"b"]
                assert underlying.reads == 1  # 不预读、不重复消费
                # 流读完 → 真实终态完成一次。
                assert len(logs) == 1
                # once 保护：重复 end/finish 不再记录。
                span.end()
                lifecycle.finish(complete=True)
                assert len(logs) == 1

            with tracer.start_as_current_span(
                "GET host /p", kind=SpanKind.CLIENT
            ) as span2:
                lifecycle2 = ins._ClientLifecycle(span2)
                span2._lifecycle = lifecycle2
                span2.end()

                class Broken(httpx.SyncByteStream):
                    def __iter__(self):
                        raise RuntimeError("stream " + SECRET_EXC)

                with pytest.raises(RuntimeError):
                    list(ins._SyncStream(Broken(), lifecycle2))
                assert len(logs) == 2
                assert logs[1]["nkb.http.outcome"] == "failure"
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


@pytest.mark.asyncio
async def test_async_stream_unit_lifecycle(monkeypatch):
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_CLIENT_COMPLETED_EVENT
    )
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter([])))
    try:
        tracer = ins._FilteringTracer(provider.get_tracer("unit-async"))
        with tracer.start_as_current_span("parent"):
            with tracer.start_as_current_span(
                "GET host /p", kind=SpanKind.CLIENT
            ) as span:
                lifecycle = ins._ClientLifecycle(span)
                span._lifecycle = lifecycle
                span.end()
                assert logs == []

                class Underlying(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b"a"
                        yield b"b"

                stream = ins._AsyncStream(Underlying(), lifecycle)
                collected = [chunk async for chunk in stream]
                assert collected == [b"a", b"b"]
                assert len(logs) == 1

            with tracer.start_as_current_span(
                "GET host /p", kind=SpanKind.CLIENT
            ) as span2:
                lifecycle2 = ins._ClientLifecycle(span2)
                span2._lifecycle = lifecycle2
                span2.end()
                await ins._AsyncStream(Underlying(), lifecycle2).aclose()
                assert len(logs) == 2
                assert logs[1]["nkb.http.outcome"] == "unknown"
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def test_exporter_self_traffic_excluded(instrumented):
    with httpx.Client(timeout=0.5) as client:
        with pytest.raises(httpx.HTTPError):
            client.post("http://127.0.0.1:4317/v1/traces", content=b"would-be-export")
    for span in _client_spans(instrumented.spans):
        assert "4317" not in span.name
        assert "4317" not in str(_attrs(span))


def test_self_traffic_exclusion_env(monkeypatch):
    monkeypatch.delenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", raising=False)
    ins._set_httpx_self_traffic_exclusion(_Settings())
    env = __import__("os").environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"]
    assert "127\\.0\\.0\\.1:4317" in env
    monkeypatch.setenv("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "existing-pattern")
    ins._set_httpx_self_traffic_exclusion(_Settings())
    assert __import__("os").environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"].startswith(
        "existing-pattern,"
    )


# ---------------------------------------------------------------------------
# 出站路由模板投影
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("/api/embed", "/api/embed"),
        ("/v1/models", "/v1/models"),
        ("/collections/n_kb_documents/points/search", "/collections/{id}/points/search"),
        ("/collections/COLL-1/points/abc123", "/collections/{id}/points/{id}"),
        ("/token/private-credential/status", "/token/{redacted}/status"),
        ("/search/alice@example.com/results", "/search/{id}/results"),
        ("/v1/models?token=PRIVATE#PRIVATE", "/v1/models"),
    ],
)
def test_http_route_template(monkeypatch, path, template):
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    assert ins._http_route_template(path) == template


def test_http_route_template_bounds_length(monkeypatch):
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    assert len(ins._http_route_template("/catalog" * 1000)) <= 512


def test_caller_identifies_application_function_without_reading_arguments():
    namespace = {"__name__": "app.infrastructure.vector.qdrant_index", "observe": ins._http_caller}
    exec("def search(secret):\n    return observe()\n", namespace)
    assert namespace["search"]("PRIVATE") == "qdrant.request"


# ---------------------------------------------------------------------------
# T12: W3C 传播隔离（空 Context 起点、畸形输入、tracestate、采样继承、不串链）
# ---------------------------------------------------------------------------


_REMOTE_TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
_REMOTE_SPAN_ID = 0x00F067AA0BA902B7


def _traceparent(trace_id: int, span_id: int, flag: str = "01") -> str:
    return f"00-{trace_id:032x}-{span_id:016x}-{flag}"


def test_missing_traceparent_yields_fresh_root_server_span(instrumented):
    """缺失 traceparent：空 Context 起点产生有效根 SERVER span，不抛出。"""
    with TestClient(instrumented.app) as client:
        response = client.post("/retrieval/search")
    assert response.status_code == 200
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1
    span = servers[0]
    assert span.context.trace_id != 0
    assert span.context.span_id != 0
    assert span.parent is None


@pytest.mark.parametrize(
    "malformed",
    [
        "not-a-traceparent",
        "",
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",
        "zz-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",
    ],
)
def test_malformed_traceparent_rejected_without_chaining(instrumented, malformed):
    """畸形 traceparent：不抛出、请求正常处理、不继承畸形值（重新起根）。"""
    with TestClient(instrumented.app) as client:
        response = client.post(
            "/retrieval/search", headers={"traceparent": malformed}
        )
    assert response.status_code == 200
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1
    span = servers[0]
    assert span.context.trace_id != 0
    assert span.context.trace_id != _REMOTE_TRACE_ID
    assert span.parent is None
    # 畸形原文不进入 span 属性/名称。
    if malformed:
        assert malformed not in json.dumps(
            {"name": span.name, "attrs": dict(span.attributes or {})}, default=str
        )


def test_tracestate_preserved_on_sampled_server_span(instrumented):
    """有效 tracestate 记录在 SERVER span context 上，不丢弃、不改写。"""
    headers = {
        "traceparent": _traceparent(_REMOTE_TRACE_ID, _REMOTE_SPAN_ID, "01"),
        "tracestate": "nkbvendor=1",
    }
    with TestClient(instrumented.app) as client:
        response = client.post("/retrieval/search", headers=headers)
    assert response.status_code == 200
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 1
    span = servers[0]
    assert format(span.context.trace_id, "032x") == format(_REMOTE_TRACE_ID, "032x")
    assert span.context.trace_state.get("nkbvendor") == "1"


def test_unsampled_remote_parent_keeps_log_context_linkage(monkeypatch):
    """未采样远端父级：无导出 span，但完成日志携带远端 trace_id 与新 span_id；两次请求不串链。"""
    monkeypatch.setattr(obs_logging, "_sanitizer", obs_logging.LogSanitizer())
    logs, handler, logger = _capture_http_logs(
        monkeypatch, obs_logging.HTTP_COMPLETED_EVENT
    )
    app = FastAPI()

    @app.post("/retrieval/search")
    async def search():
        return {"results": []}

    sampler = ParentBased(ALWAYS_OFF)
    provider = TracerProvider(sampler=sampler)
    spans: list = []
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    assert ins.instrument_app(app, settings=_Settings(), tracer_provider=provider)

    remote_ids = [_REMOTE_TRACE_ID, 0x1234567890ABCDEF1234567890ABCDEF]
    try:
        with TestClient(app) as client:
            for trace_id in remote_ids:
                headers = {"traceparent": _traceparent(trace_id, _REMOTE_SPAN_ID, "00")}
                response = client.post("/retrieval/search", headers=headers)
                assert response.status_code == 200
        assert len(logs) == 2
        for log, trace_id in zip(logs, remote_ids):
            assert log["trace_id"] == format(trace_id, "032x")
            assert int(log["span_id"], 16) != 0
        assert logs[0]["span_id"] != logs[1]["span_id"]
        assert logs[0]["trace_id"] != logs[1]["trace_id"]
        # 未采样：不要求导出 span。
        assert not spans
    finally:
        logger.removeHandler(handler)


def test_exception_request_does_not_poison_next_trace(instrumented):
    """异常请求后下一请求重新起根，不继承异常请求的 trace。"""
    poisoned = _traceparent(0x11111111111111111111111111111111, _REMOTE_SPAN_ID, "01")
    with TestClient(instrumented.app, raise_server_exceptions=False) as client:
        failed = client.post("/documents/boom/index", headers={"traceparent": poisoned})
        following = client.get("/documents")
    assert failed.status_code == 500
    assert following.status_code == 200
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 2
    success = next(s for s in servers if s.name.startswith("GET"))
    assert format(success.context.trace_id, "032x") != format(
        0x11111111111111111111111111111111, "032x"
    )
    assert success.parent is None


@pytest.mark.asyncio
async def test_cancelled_request_does_not_poison_next_trace(instrumented):
    """取消请求后：下一请求带有效父级则继承之，无父级则重新起根。"""
    app = instrumented.app
    gate = asyncio.Event()
    hang = {"active": False}

    class _GateApp:
        async def __call__(self, scope, receive, send):
            if hang["active"]:
                await gate.wait()
            await app(scope, receive, send)

    wrapped = _GateApp()

    async def call_asgi(asgi_app, traceparent=None):
        headers = [(b"host", b"test"), (b"content-type", b"application/json")]
        if traceparent:
            headers.append((b"traceparent", traceparent.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/retrieval/search",
            "raw_path": b"/retrieval/search",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("test", 123),
            "headers": headers,
        }
        request_body_sent = asyncio.Event()

        async def receive():
            if not request_body_sent.is_set():
                request_body_sent.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        await asgi_app(scope, receive, send)

    poisoned = _traceparent(0x22222222222222222222222222222222, _REMOTE_SPAN_ID, "01")
    hang["active"] = True
    cancelled_task = asyncio.ensure_future(call_asgi(wrapped, poisoned))
    await asyncio.sleep(0.05)
    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    hang["active"] = False
    gate.set()

    instrumented.spans.clear()
    inherited = _traceparent(0x33333333333333333333333333333333, _REMOTE_SPAN_ID, "01")
    await call_asgi(wrapped, inherited)
    await call_asgi(wrapped)
    servers = _server_spans(instrumented.spans)
    assert len(servers) == 2
    by_trace = {format(s.context.trace_id, "032x"): s for s in servers}
    assert "33333333333333333333333333333333" in by_trace
    fresh = [t for t in by_trace if t != "33333333333333333333333333333333"]
    assert len(fresh) == 1
    assert by_trace[fresh[0]].parent is None
    assert "22222222222222222222222222222222" not in by_trace


@pytest.mark.asyncio
async def test_concurrent_requests_keep_distinct_remote_traces(instrumented):
    """并发请求各自继承自己的远端父级，不交叉串链。"""
    app = instrumented.app
    count = 5
    trace_ids = [0xA0000000000000000000000000000001 + i for i in range(count)]

    async def call_asgi(trace_id):
        headers = [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"traceparent", _traceparent(trace_id, _REMOTE_SPAN_ID, "01").encode()),
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/retrieval/search",
            "raw_path": b"/retrieval/search",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("test", 123),
            "headers": headers,
        }
        request_body_sent = asyncio.Event()

        async def receive():
            if not request_body_sent.is_set():
                request_body_sent.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        await app(scope, receive, send)

    await asyncio.gather(*(call_asgi(trace_id) for trace_id in trace_ids))
    servers = _server_spans(instrumented.spans)
    assert len(servers) == count
    span_traces = [format(s.context.trace_id, "032x") for s in servers]
    assert sorted(span_traces) == sorted(format(t, "032x") for t in trace_ids)
    assert len(set(span_traces)) == count
    for span in servers:
        assert format(span.parent.span_id, "016x") == format(_REMOTE_SPAN_ID, "016x")


def test_global_propagator_untouched_tracecontext_enabled():
    """不修改全局 propagator：埋点前后同一实例且含 TraceContext；本模块不注入 baggage/不改部署。"""
    from opentelemetry.propagate import extract, get_global_textmap

    before = get_global_textmap()
    app = build_app()
    assert ins.instrument_app(app, settings=_Settings(), tracer_provider=TracerProvider())
    # 幂等注册（模块 fixture 已全局注册过），此处只为验证 propagator 不被改写。
    ins.install_global_instrumentors(settings=_Settings(), tracer_provider=TracerProvider())
    after = get_global_textmap()
    assert after is before
    fields = set(after.fields)
    assert "traceparent" in fields
    # 行为证明：全局 propagator 提取远端 traceparent 得到有效远端上下文（tracecontext 启用）。
    remote_context = trace_api.get_current_span(
        extract({"traceparent": _traceparent(_REMOTE_TRACE_ID, _REMOTE_SPAN_ID)})
    ).get_span_context()
    assert remote_context.is_valid and remote_context.is_remote
    assert format(remote_context.trace_id, "032x") == format(_REMOTE_TRACE_ID, "032x")
    # 本模块源码不调用 set_global_textmap、不注入业务 baggage 传播。
    for module in (ins, obs_logging):
        source = inspect.getsource(module)
        assert "set_global_textmap" not in source
        assert "W3CBaggagePropagator" not in source
    # 部署样例不覆盖 OTEL_PROPAGATORS（部署默认启用 tracecontext）。
    for deploy_file in (
        Path(__file__).resolve().parents[3] / "docker" / ".env.example",
        Path(__file__).resolve().parents[3] / "docker" / "docker-compose.yml.example",
        Path(__file__).resolve().parents[3] / "docker" / "observability" / "compose.yml",
    ):
        if deploy_file.exists():
            assert "OTEL_PROPAGATORS" not in deploy_file.read_text(encoding="utf-8")


def test_http_status_mapping_server_4xx_unset_5xx_error():
    """SERVER span：4xx 保持 UNSET、5xx 为 ERROR（spec：客户端输入错误
    不映射为服务端故障）；CLIENT 侧 4xx/5xx 均为 ERROR。"""
    spans: list = []
    app = FastAPI()

    @app.post("/retrieval/search")
    async def search(mode: str = "ok"):
        if mode == "client_error":
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": {"code": "invalid_query"}}, status_code=422)
        raise RuntimeError("boom")

    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    assert ins.instrument_app(app, settings=_Settings(), tracer_provider=provider)

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/retrieval/search?mode=client_error").status_code == 422
        assert client.post("/retrieval/search?mode=server_error").status_code == 500

    server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(server_spans) == 2
    by_status_code = {s.status.status_code for s in server_spans}
    assert StatusCode.UNSET in by_status_code
    assert StatusCode.ERROR in by_status_code

    # CLIENT（httpx，pinned 0.66b1 语义）：4xx/5xx 均映射 ERROR。
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    client_spans: list = []
    client_provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    client_provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(client_spans)))
    for response_status in (422, 503):
        http_client = httpx.Client(transport=httpx.MockTransport(
            lambda request, s=response_status: httpx.Response(s)
        ))
        HTTPXClientInstrumentor.instrument_client(
            http_client,
            tracer_provider=ins._FilteringTracerProvider(client_provider),
            request_hook=ins._httpx_request_hook,
            response_hook=ins._httpx_response_hook,
        )
        try:
            # CLIENT 门控要求有效父级，无父级直接返回 INVALID_SPAN。
            with client_provider.get_tracer("test").start_as_current_span("parent"):
                assert http_client.get("https://models.example/v1/models").status_code == response_status
        finally:
            http_client.close()
    client_span_statuses = {
        s.status.status_code for s in client_spans if s.kind == SpanKind.CLIENT
    }
    assert client_span_statuses == {StatusCode.ERROR}


def test_system_metrics_are_idempotent_and_failures_are_isolated(monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    class Instrumentor:
        def instrument(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setitem(sys.modules, 'opentelemetry.instrumentation.system_metrics',
                        SimpleNamespace(SystemMetricsInstrumentor=Instrumentor))
    ins._reset_for_tests()
    provider = object()
    assert getattr(ins, 'instrument_system_metrics', lambda _:False)(provider)
    assert ins.instrument_system_metrics(provider)
    assert calls == [{'meter_provider':provider}]
    ins._reset_for_tests()
    def broken():
        raise RuntimeError('SECRET')
    monkeypatch.setitem(sys.modules, 'opentelemetry.instrumentation.system_metrics',
                        SimpleNamespace(SystemMetricsInstrumentor=broken))
    assert ins.instrument_system_metrics(provider) is False


def test_allowlist_reads_effective_routes_in_lazy_router_nodes(monkeypatch):
    """FastAPI 0.137+ includes have no path/methods on the top-level node."""
    from fastapi import routing
    monkeypatch.delattr(routing, "iter_route_contexts", raising=False)
    route = SimpleNamespace(path='/api/retrieval/search', methods={'POST'})
    branch = SimpleNamespace(effective_route_contexts=lambda: iter([route]))
    app = SimpleNamespace(routes=[branch])
    allowlist, templates = ins._derive_allowlist(app, _Settings())
    assert allowlist.allows('POST', '/api/retrieval/search')
    assert not allowlist.allows('GET', '/api/retrieval/search')
    assert not allowlist.allows('POST', '/retrieval/search')
    assert templates == {'/api/retrieval/search'}


@pytest.mark.parametrize('prefix', ['', '/api'])
@pytest.mark.parametrize('sampled', [True, False])
def test_included_router_keeps_remote_context_and_single_server(prefix, sampled):
    from fastapi import APIRouter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()
    inner = APIRouter()

    @inner.post('/search')
    def search():
        with provider.get_tracer('included-router-regression').start_as_current_span('business'):
            context = trace_api.get_current_span().get_span_context()
            return {'trace_id': format(context.trace_id, '032x'), 'sampled': context.trace_flags.sampled}

    outer = APIRouter()
    outer.include_router(inner, prefix='/retrieval')
    app.include_router(outer, prefix=prefix)
    assert ins.instrument_app(app, settings=_Settings(), tracer_provider=provider)
    trace_id = '04a0c0087de9bb79f59ad7704a7f2889'
    parent_id = '02bda11c976ac3e0'
    try:
        with TestClient(app) as client:
            response = client.post(prefix + '/retrieval/search', headers={
                'traceparent': f'00-{trace_id}-{parent_id}-' + ('01' if sampled else '00'),
                'tracestate': 'probe=keep',
            })
        assert response.status_code == 200
        assert response.json() == {'trace_id':trace_id, 'sampled':sampled}
        servers = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
        if sampled:
            assert len(servers) == 1
            assert servers[0].parent.span_id == int(parent_id, 16)
            assert servers[0].context.trace_state.get('probe') == 'keep'
            assert servers[0].name == 'POST ' + prefix + '/retrieval/search'
        else:
            assert not exporter.get_finished_spans()
    finally:
        provider.shutdown()


@pytest.mark.parametrize("mode,expected", [("success", "success"), ("denial", "denied"), ("timeout", "timed_out")])
def test_sync_route_result_reaches_http_owner_without_spoofing(instrumented, monkeypatch, mode, expected):
    from app.interfaces.http.errors import run_with_error_mapping, register_error_handlers
    logs, handler, logger = _capture_http_logs(monkeypatch, obs_logging.HTTP_COMPLETED_EVENT)
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/retrieval/search")
    def search():
        def action():
            if mode == "denial":
                raise ValueError("file_too_large")
            if mode == "timeout":
                raise TimeoutError()
            return {"results": []}
        return run_with_error_mapping(action)

    provider = TracerProvider(sampler=ALWAYS_ON)
    spans = []
    provider.add_span_processor(SimpleSpanProcessor(_ListSpanExporter(spans)))
    ins.instrument_app(app, settings=_Settings(), tracer_provider=provider)
    try:
        with TestClient(app) as client:
            client.post("/retrieval/search", headers={"nkb.request.channel": "mcp_stdio", "baggage": "nkb.request.id=forged"})
        assert len(logs) == 1
        assert logs[0]["nkb.request.outcome"] == expected
        assert logs[0]["nkb.request.channel"] == "api"
        assert "nkb.request.id" not in logs[0]
        span = next(s for s in spans if s.kind == SpanKind.SERVER)
        event = next(e for e in span.events if e.name == "nkb.http.completed")
        assert event.attributes["nkb.request.outcome"] == expected
        assert span.attributes["nkb.request.outcome"] == expected
        for key in ("nkb.http.outcome", "nkb.http.duration_ms", "nkb.request.duration_ms"):
            assert event.attributes[key] == span.attributes[key] == logs[0][key]
        assert not any(s.name == "request.handle" for s in spans)
    finally:
        logger.removeHandler(handler)
        provider.shutdown()
