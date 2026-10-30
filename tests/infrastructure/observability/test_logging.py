"""T5: 事件注册表、安全日志桥与 JSON stderr 出口合同测试。

覆盖 plan-261029-otel-observability T5 第 7/8 条：

- 事件注册表精确模板匹配（P058：枚举键、无前缀放行）；
- 放行 getMessage 仅限已注册模板（fail closed），未注册记录折叠为事件码；
- exc_info 只投影 exception.type，正文与堆栈不出任何出口；
- OTel 日志记录出口（真实 SDK LoggingHandler + InMemoryLogExporter）：
  body 使用事件码/投影消息，trace/span id 来自当前有效 context；
- JSON stderr 出口：固定键、既有 handler 不改写并输出边界诊断、
  模块自有 handler 幂等安装、回收只移除自有 handler、stdout 零日志；
- httpx/httpcore/opentelemetry/grpc INFO 降噪与桥拒绝；
- 自检 ring buffer / snapshot 出口秘密哨兵（SQL、URL、exc_info）。
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3

import httpx
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider

from app.config import Settings
from app.infrastructure.observability import logging as obs
from app.infrastructure.observability import setup as obs_setup

SENTINEL_EXC = "SECRET-EXC-sentinel-T5"
SENTINEL_SQL = "SECRET-SQL-sentinel-T5"
SENTINEL_URL = "SECRET-URL-sentinel-T5"


class _SpyOtelHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _StringJsonHandler(obs.ObservabilityJsonHandler):
    def __init__(self):
        super().__init__(stream=io.StringIO())

    @property
    def text(self):
        return self.stream.getvalue()

    @property
    def payloads(self):
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _reset_state():
    obs._reset_for_tests()
    yield
    obs._reset_for_tests()


def _sanitized(monkeypatch):
    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog((SENTINEL_EXC, SENTINEL_SQL, SENTINEL_URL))
    monkeypatch.setattr(obs, "_sanitizer", sanitizer)
    return sanitizer


# ---------------------------------------------------------------------------
# 事件注册表（精确模板枚举）
# ---------------------------------------------------------------------------


def test_event_code_registry_exact_match():
    obs.register_safe_event("business done %s", "business_done")
    obs.register_safe_event("business done again", "business_done_again")
    assert obs.event_code_for_template("business done %s") == "business_done"
    assert obs.event_code_for_template("business done") is None
    assert obs.event_code_for_template("business done again extra") is None
    assert obs.event_code_for_template("business done %s extra") is None
    assert obs.event_code_for_template("business don") is None
    assert obs.event_code_for_template("business done") is None
    assert obs.event_code_for_template("") is None
    assert obs.event_code_for_template("business done %s") == "business_done"
    # 重复注册同模板同事件码幂等
    obs.register_safe_event("business done %s", "business_done")
    assert obs.event_code_for_template("business done %s") == "business_done"


def test_event_code_for_record_prefers_registered_template():
    obs.register_safe_event("http_request_completed %s", "http_request_completed")
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1,
        "http_request_completed %s", ("x",), None,
    )
    assert obs.event_code_for(record) == "http_request_completed"
    unregistered = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "unregistered %s", ("y",), None,
    )
    assert obs.event_code_for(unregistered) == obs.UNKNOWN_EVENT_CODE
    assert obs.event_code_for(None) == obs.UNKNOWN_EVENT_CODE


# ---------------------------------------------------------------------------
# 访问事实日志（ContextVar 事实，SERVER 仍 current 时发送）
# ---------------------------------------------------------------------------


def test_log_http_request_completed_facts_and_sanitization(monkeypatch):
    _sanitized(monkeypatch)
    payloads = []
    handler = _StringJsonHandler()
    logger = logging.getLogger(obs.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "_cache", {})
    logger.addHandler(handler)
    try:
        provider = TracerProvider()
        span = provider.get_tracer("t").start_span(
            "POST /retrieval/search", kind=trace_api.SpanKind.SERVER
        )
        try:
            with trace_api.use_span(span, end_on_exit=False):
                obs.log_http_request_completed(
                    "POST", "/retrieval/search", 200, 12.3, "completed"
                )
                obs.log_http_request_completed(
                    "GET", "/documents/{document_id}", None, 0.5, "cancelled"
                )
        finally:
            span.end()
        first, second = handler.payloads
        assert first["event"] == "http_request_completed"
        assert first["http.request.method"] == "POST"
        assert first["http.route"] == "/retrieval/search"
        assert first["http.response.status_code"] == 200
        assert first["duration_ms"] == 12.3
        assert first["outcome"] == "completed"
        assert first["trace_id"] == format(span.get_span_context().trace_id, "032x")
        assert first["span_id"] == format(span.get_span_context().span_id, "016x")
        assert "http.response.status_code" not in second
        assert second["outcome"] == "cancelled"
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def test_log_http_client_completed_facts(monkeypatch):
    _sanitized(monkeypatch)
    handler = _StringJsonHandler()
    logger = logging.getLogger(obs.__name__)
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "_cache", {})
    logger.addHandler(handler)
    try:
        provider = TracerProvider()
        span = provider.get_tracer("t").start_span(
            "GET models", kind=trace_api.SpanKind.CLIENT
        )
        try:
            with trace_api.use_span(span, end_on_exit=False):
                obs.log_http_client_completed(
                    "GET", "models.example", 443, "/v1/models", 200,
                    "app.infrastructure.vector.qdrant_index:search",
                )
        finally:
            span.end()
        payload = handler.payloads[0]
        assert payload["event"] == "http_client_completed"
        assert payload["http.request.method"] == "GET"
        assert payload["server.address"] == "models.example"
        assert payload["server.port"] == 443
        assert payload["url.template"] == "/v1/models"
        assert payload["http.response.status_code"] == 200
        assert payload["outcome"] == "completed"
        assert payload["span_id"] == format(span.get_span_context().span_id, "016x")
        assert payload["trace_id"] == format(span.get_span_context().trace_id, "032x")
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


# ---------------------------------------------------------------------------
# 安全桥投影（SDK 接收前完成）
# ---------------------------------------------------------------------------


def test_bridge_registered_template_releases_message(monkeypatch):
    _sanitized(monkeypatch)
    obs.register_safe_event("business done %s", "business_done")
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("obs-bridge-test")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    try:
        log.info("business done %s", "payload-42")
    finally:
        log.removeHandler(bridge)
    record = spy.records[0]
    assert record.msg == "business done payload-42"
    assert record.args == ()
    assert record.exc_info is None
    assert record.levelno == logging.INFO
    assert record.name == "obs-bridge-test"


def test_bridge_unregistered_readable_and_drops_raw_exc_info(monkeypatch):
    _sanitized(monkeypatch)
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("obs-bridge-test")
    log.addHandler(bridge)
    log.setLevel(logging.DEBUG)
    try:
        log.info("unregistered body %s", SENTINEL_EXC)
        log.error("unregistered error", exc_info=ValueError(SENTINEL_EXC))
    finally:
        log.removeHandler(bridge)
    assert len(spy.records) == 2
    record = spy.records[1]
    assert record.msg == "unregistered error"
    assert record.args == ()
    assert record.exc_info is None
    assert SENTINEL_EXC not in repr(record.__dict__)
    raw = logging.LogRecord(
        "obs-bridge-test", logging.ERROR, __file__, 1,
        "unregistered error", (), ValueError(SENTINEL_EXC),
    )
    prepared = obs.project_record(raw)
    assert prepared.message == "unregistered error"
    assert prepared.exception_type == "ValueError"
    assert prepared.exception["message"] == "[REDACTED]"
    assert SENTINEL_EXC not in json.dumps(prepared.details, default=str)
    assert SENTINEL_EXC not in str(prepared.exception_type)


def test_bridge_adds_http_client_completed_event_on_current_span(monkeypatch):
    _sanitized(monkeypatch)
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("obs-bridge-test")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    provider = TracerProvider()
    span = provider.get_tracer("t").start_span("GET x", kind=trace_api.SpanKind.CLIENT)
    try:
        with trace_api.use_span(span, end_on_exit=False):
            log.info("http_client_completed %s", "facts")
        events = [e for e in span.events if e.name == "http_client_completed"]
        assert len(events) == 1
        assert events[0].attributes["message"] == "http_client_completed facts"
    finally:
        log.removeHandler(bridge)
        span.end()
        provider.shutdown()


def test_bridge_emit_failure_isolated_per_arm(monkeypatch):
    _sanitized(monkeypatch)
    obs.register_safe_event("business done", "business_done")

    class Broken(logging.Handler):
        def emit(self, record):
            raise RuntimeError("otel sink broken")

    bridge = obs.SafeLogBridge(otel_handler=Broken())
    log = logging.getLogger("obs-bridge-test")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    try:
        log.info("business done")  # 桥内异常不得传播
    finally:
        log.removeHandler(bridge)


def test_bridge_rejects_self_traffic(monkeypatch):
    _sanitized(monkeypatch)
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    for name in ("opentelemetry.sdk.trace", "grpc._server", "opentelemetry"):
        log = logging.getLogger(name)
        log.addHandler(bridge)
        log.setLevel(logging.DEBUG)
        try:
            log.warning("internal warning")
        finally:
            log.removeHandler(bridge)
    assert spy.records == []


# ---------------------------------------------------------------------------
# OTel 日志出口（真实 SDK LoggingHandler）
# ---------------------------------------------------------------------------


# SDK 的 InMemoryLogExporter/LoggingHandler 自带 DeprecationWarning（仍为
# 当前可用的记录出口实现），仅在本测试内忽略，避免污染全局验收。
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_otel_log_outlet_body_ids_and_severity(monkeypatch):
    _sanitized(monkeypatch)
    obs.register_safe_event("unit done %s", "unit_done")
    exporter = InMemoryLogExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    tp = TracerProvider()
    span = tp.get_tracer("t").start_span("op")
    bridge = obs.SafeLogBridge(
        otel_handler=LoggingHandler(level=logging.INFO, logger_provider=lp)
    )
    log = logging.getLogger("obs-otel-test")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    try:
        with trace_api.use_span(span, end_on_exit=False):
            log.warning("unit done %s", "ok-value")
            log.info("unregistered body " + SENTINEL_URL)
    finally:
        log.removeHandler(bridge)
        span.end()
        lp.shutdown()
        tp.shutdown()
    logs = exporter.get_finished_logs()
    assert len(logs) == 2
    assert logs[1].log_record.attributes["event"] == "library_log"
    log_record = logs[0].log_record
    assert log_record.body == "unit done ok-value"
    assert format(log_record.trace_id, "032x") == format(
        span.get_span_context().trace_id, "032x"
    )
    assert format(log_record.span_id, "016x") == format(
        span.get_span_context().span_id, "016x"
    )
    assert SENTINEL_URL not in json.dumps(str(log_record.body))


# ---------------------------------------------------------------------------
# JSON stderr 出口
# ---------------------------------------------------------------------------


def test_json_formatter_fixed_keys_and_exception_type_only():
    formatter = obs.SafeEventJsonFormatter()
    record = logging.LogRecord(
        "app.test", logging.ERROR, __file__, 1, "diagnostic_log",
        None, ValueError(SENTINEL_EXC),
    )
    payload = json.loads(formatter.format(record))
    assert set(payload) <= {
        "timestamp", "severity", "logger", "event", "message", "details", "details_unavailable",
        "exception", "trace_id", "span_id", "service.name", "service.instance.id",
    }
    assert payload["event"] == "diagnostic_log"
    assert payload["exception"] == {"type": "ValueError"}
    assert SENTINEL_EXC not in json.dumps(payload)
    assert payload["service.name"] == "n-kb"


def test_json_handler_filters_by_prepared_record():
    handler = _StringJsonHandler()
    handler.setFormatter(obs.SafeEventJsonFormatter())
    log = logging.getLogger("obs-json-test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("HTTP Request: GET http://host/" + SENTINEL_URL)
    finally:
        log.removeHandler(handler)
    assert handler.text == ""


class _ForeignHandler(logging.Handler):
    def emit(self, record):
        pass


def test_attach_idempotent_keeps_foreign_and_diagnoses(capsys):
    foreign = _ForeignHandler()
    log = logging.getLogger("obs-attach-test")
    log.addHandler(foreign)
    try:
        assert obs.attach_observability_log_handlers(root=log) is True
        assert obs.attach_observability_log_handlers(root=log) is True
        owned = [
            h for h in log.handlers
            if getattr(h, "_nkb_observability_owned", False)
        ]
        assert len(owned) == 2
        assert foreign in log.handlers
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "handler" in captured.err.lower()
        # 走一遍 JSON stderr 输出
        log.warning("registered warning")
        assert "registered" not in captured.out
        err = capsys.readouterr().err
        payload = json.loads(err.splitlines()[-1])
        assert payload["event"] == "diagnostic_log"
        assert payload["message"] is None  # 未注册模板不放行正文
    finally:
        obs.detach_observability_log_handlers(root=log)
        log.removeHandler(foreign)


def test_attach_creates_json_and_bridge_handlers():
    log = logging.getLogger("obs-attach-test")
    assert obs.attach_observability_log_handlers(root=log) is True
    try:
        kinds = [type(h).__name__ for h in log.handlers]
        assert "ObservabilityJsonHandler" in kinds
        assert "SafeLogBridge" in kinds
    finally:
        obs.detach_observability_log_handlers(root=log)
    assert log.handlers == []


def test_facts_survive_default_log_levels(monkeypatch, capsys):
    """回归：装配必须自行把事实 logger 提到 INFO。

    默认 root WARNING（事实 logger NOTSET）会把
    http_request_completed/http_client_completed 静默吞掉；测试不得
    monkeypatch 事实 logger 级别来掩盖该缺陷。
    """
    _sanitized(monkeypatch)
    root = logging.getLogger("app")
    old_level = root.level
    facts_logger = logging.getLogger(obs.__name__)
    old_facts_level = facts_logger.level
    root.setLevel(logging.WARNING)
    facts_logger.setLevel(logging.NOTSET)
    try:
        assert obs.attach_observability_log_handlers(root=root) is True
        assert facts_logger.getEffectiveLevel() == logging.INFO
        obs.log_http_request_completed(
            "GET", "/documents/{document_id}", 200, 1.0, "completed"
        )
        obs.log_http_client_completed(
            "GET", "models.example", 443, "/v1/models", 200,
            "app.infrastructure.vector.qdrant_index:search", 3.5,
        )
        err = capsys.readouterr().err
        payloads = [json.loads(line) for line in err.splitlines() if line.strip()]
        events = [p["event"] for p in payloads]
        assert "http_request_completed" in events
        assert "http_client_completed" in events
        client_payload = next(
            p for p in payloads if p["event"] == "http_client_completed"
        )
        assert client_payload["duration_ms"] == 3.5
    finally:
        obs.detach_observability_log_handlers(root=root)
        root.setLevel(old_level)
        facts_logger.setLevel(old_facts_level)


def test_detach_removes_only_owned_handlers():
    foreign = _ForeignHandler()
    log = logging.getLogger("obs-attach-test")
    log.addHandler(foreign)
    obs.attach_observability_log_handlers(root=log)
    obs.detach_observability_log_handlers(root=log)
    try:
        assert log.handlers == [foreign]
    finally:
        log.removeHandler(foreign)


def test_bridge_default_attaches_via_setup_bridge():
    class _FakeBridge:
        level = logging.DEBUG

        def __init__(self):
            self.records = []

        def handle(self, record):
            self.records.append(record)

    fake = _FakeBridge()
    log = logging.getLogger("obs-attach-test")
    old_level = log.level
    log.setLevel(logging.INFO)
    assert obs.attach_observability_log_handlers(root=log, bridge=fake) is True
    try:
        log.info("anything %s", "x")
        assert len(fake.records) == 1
    finally:
        obs.detach_observability_log_handlers(root=log)
        log.setLevel(old_level)


# ---------------------------------------------------------------------------
# 库日志降噪
# ---------------------------------------------------------------------------


def test_httpx_info_suppressed_in_both_outlets(monkeypatch):
    handler = _StringJsonHandler()
    handler.setFormatter(obs.SafeEventJsonFormatter())
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("httpx")
    monkeypatch.setattr(log, "level", logging.INFO)
    monkeypatch.setattr(log, "_cache", {})
    log.addHandler(handler)
    log.addHandler(bridge)
    try:
        log.info("HTTP Request: GET https://host/path?" + SENTINEL_URL)
    finally:
        log.removeHandler(handler)
        log.removeHandler(bridge)
    assert handler.text == ""
    assert spy.records == []


def test_httpx_warning_keeps_event_but_not_body():
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("httpcore")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    try:
        log.warning("connection failed " + SENTINEL_URL)
    finally:
        log.removeHandler(bridge)
    assert len(spy.records) == 1
    assert spy.records[0].msg == obs.UNKNOWN_EVENT_CODE
    assert SENTINEL_URL not in spy.records[0].msg


def test_opentelemetry_logger_rejected_from_bridge():
    spy = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(otel_handler=spy)
    log = logging.getLogger("opentelemetry.exporter.otlp")
    log.addHandler(bridge)
    log.setLevel(logging.INFO)
    try:
        log.warning("export failed")
    finally:
        log.removeHandler(bridge)
    assert spy.records == []


# ---------------------------------------------------------------------------
# ring buffer / snapshot 出口秘密哨兵
# ---------------------------------------------------------------------------


def _setup_capturing_global_providers(monkeypatch):
    """真实 SDK 装配，但捕获全局 Provider 发布，避免跨测试污染。

    OTel API 的全局 Provider 进程内只能发布一次，且外部已发布的
    provider 会让装配跳过自有发布（顺序敏感污染）。这里以真实 SDK
    类为基础，仅把 set/get_*_provider 换成捕获桩：装配恒定执行、
    全局 API 不被触碰，测试从捕获结果取真实 provider。
    """
    published = {"tracer": [], "meter": [], "logger": []}
    loader = obs_setup._default_sdk_loader()
    loader.set_tracer_provider = published["tracer"].append
    loader.get_tracer_provider = lambda: None
    loader.set_meter_provider = published["meter"].append
    loader.get_meter_provider = lambda: None
    loader.set_logger_provider = published["logger"].append
    loader.get_logger_provider = lambda: None
    settings = Settings(
        _env_file=None, otel_enabled=True,
        otel_exporter_otlp_endpoint="http://127.0.0.1:4317",
    )
    handle = obs_setup.setup_observability(settings, sdk_loader=loader)
    assert handle is not None
    return handle, published


def test_ring_buffer_and_snapshot_reject_secrets(monkeypatch):
    handle, published = _setup_capturing_global_providers(monkeypatch)
    try:
        provider = published["tracer"][0]
        with provider.get_tracer("t").start_as_current_span("parent") as span:
            span.set_attribute("db.statement", f"SELECT '{SENTINEL_SQL}'")
            span.set_attribute("url.full", f"http://host/x?{SENTINEL_URL}")
            span.record_exception(ValueError(SENTINEL_EXC))
        snapshot = handle.snapshot()
        text = json.dumps(snapshot, ensure_ascii=False, default=str)
        assert SENTINEL_SQL not in text
        assert SENTINEL_URL not in text
        assert SENTINEL_EXC not in text
    finally:
        handle.release()
        obs_setup._reset_for_tests()


def test_json_stderr_with_real_setup(monkeypatch, capsys):
    obs.prepare_sanitizer()
    obs.register_safe_event("unit done %s", "unit_done")
    handle, _published = _setup_capturing_global_providers(monkeypatch)
    try:
        log = logging.getLogger("obs-setup-test")
        assert obs.attach_observability_log_handlers(root=log) is True
        try:
            log.warning("unit done %s", "via-bridge")
        finally:
            obs.detach_observability_log_handlers(root=log)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "via-bridge" in captured.err
        snapshot = handle.snapshot()
        text = json.dumps(snapshot, ensure_ascii=False, default=str)
        for sentinel in (SENTINEL_SQL, SENTINEL_URL, SENTINEL_EXC):
            assert sentinel not in text
    finally:
        handle.release()
        obs_setup._reset_for_tests()


# ---------------------------------------------------------------------------
# T8 回归：业务完成日志模板由生产代码登记后经注册表放行
# ---------------------------------------------------------------------------


def test_application_completion_templates_registered_by_composition(monkeypatch):
    from app.composition import register_application_log_events
    from app.application.ingestion_service import INDEX_COMPLETED_TEMPLATES
    from app.application.retrieval_service import RETRIEVAL_COMPLETED_TEMPLATES

    _sanitized(monkeypatch)

    register_application_log_events()

    for template in RETRIEVAL_COMPLETED_TEMPLATES:
        assert obs.event_code_for_template(template) == "nkb.retrieval.completed"
    for template in INDEX_COMPLETED_TEMPLATES:
        assert obs.event_code_for_template(template) == "nkb.index.completed"

    record = logging.LogRecord(
        "app.application.retrieval_service",
        logging.INFO,
        __file__,
        1,
        RETRIEVAL_COMPLETED_TEMPLATES[0],
        ("success", 1, 1.5),
        None,
    )
    prepared = obs.project_record(record)
    assert prepared.event == "nkb.retrieval.completed"
    assert prepared.message is not None and "results=1" in prepared.message

    index_record = logging.LogRecord(
        "app.application.ingestion_service",
        logging.WARNING,
        __file__,
        1,
        INDEX_COMPLETED_TEMPLATES[1],
        ("doc-1", "error", "parse:error", 2.0),
        None,
    )
    index_prepared = obs.project_record(index_record)
    assert index_prepared.event == "nkb.index.completed"
    assert index_prepared.message is not None and "outcome=error" in index_prepared.message


def test_bridge_records_once_and_isolates_failing_callback():
    from app.infrastructure.observability.snapshots import SafeLogBuffer
    buffer = SafeLogBuffer()
    sink = _SpyOtelHandler()
    bridge = obs.SafeLogBridge(sink, on_record=buffer.append)
    record = logging.LogRecord('test', logging.WARNING, '', 1, 'SECRET raw', ('SECRET args',), None)
    record.extra_secret = 'SECRET extra'
    bridge.emit(record)
    assert buffer.latest()['retained_count'] == 1
    assert 'SECRET' not in json.dumps(buffer.latest())
    def broken(item):
        raise RuntimeError('SECRET')
    bridge = obs.SafeLogBridge(sink, on_record=broken)
    bridge.emit(record)
    assert len(sink.records) == 2
    assert bridge.failure_count == 1


def test_local_and_otlp_share_safe_event_and_facts():
    from app.infrastructure.observability.snapshots import SafeLogBuffer
    buffer = SafeLogBuffer()
    sink = _SpyOtelHandler()
    record = logging.LogRecord('test', logging.INFO, '', 1, obs.HTTP_COMPLETED_EVENT + " %s", ("safe",), None)
    record.http_facts = {'http.request.method':'POST', 'http.response.status_code':200,
                         'duration_ms':3.5, 'outcome':'completed', 'query':'SECRET'}
    bridge = obs.SafeLogBridge(sink, on_record=buffer.append)
    bridge.emit(record)
    assert buffer.latest()['items'][0]['attributes'].get('http.response.status_code') == 200
    assert getattr(sink.records[0], 'event', None) == obs.HTTP_COMPLETED_EVENT
    assert sink.records[0].__dict__['http.response.status_code'] == 200
    assert 'SECRET' not in json.dumps(buffer.latest())
