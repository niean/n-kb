"""T2: app/application/telemetry_semantics.py 行为测试。

使用 opentelemetry-sdk 的内存 exporter/reader 收集真实 span 与 metric；
被测模块本身只允许依赖标准库与 opentelemetry-api（由本文件的 AST 守护测试保证）。
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app.application.telemetry_semantics as ts

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "application"
    / "telemetry_semantics.py"
)


# ---------------------------------------------------------------------------
# 收集设施：模块级一次性安装全局 SDK provider，函数级清理
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def span_exporter():
    """T8 组合顺序协调：附加到现有全局 SDK provider，或设置自有 provider。

    Application 层 T8 测试模块按字母序先于本模块执行并可能已发布全局
    provider；opentelemetry-api 的 set_tracer_provider 只生效一次，重复
    设置会被忽略。因此已有 SDK provider 时附加本模块的 processor（同一
    全局 provider 上可并存多个 processor），否则才创建并发布。
    """
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = trace.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(processor)
        trace.set_tracer_provider(owning)
    else:
        try:
            provider.add_span_processor(processor)
        except Exception:
            pass
    return exporter


@pytest.fixture(scope="module")
def reader():
    reader = InMemoryMetricReader()
    provider = metrics.get_meter_provider()
    if provider is None or type(provider).__name__ in (
        "ProxyMeterProvider",
        "_ProxyMeterProvider",
    ):
        owning = MeterProvider(metric_readers=[reader])
        metrics.set_meter_provider(owning)
    else:
        try:
            provider.add_metric_reader(reader)
        except Exception:
            pass
    return reader


@pytest.fixture(autouse=True)
def _clean_collectors(span_exporter, reader):
    span_exporter.clear()
    _clean_collectors.baseline = _snapshot(reader)
    yield
    span_exporter.clear()


# ---------------------------------------------------------------------------
# metric 快照与增量辅助
# ---------------------------------------------------------------------------


def _snapshot(reader) -> dict:
    result: dict = {}
    data = reader.get_metrics_data()
    if data is None:
        return result
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                entry = result.setdefault(metric.name, {"unit": metric.unit, "series": {}})
                for point in metric.data.data_points:
                    key = frozenset((point.attributes or {}).items())
                    if isinstance(metric.data, Histogram):
                        entry["series"][key] = {
                            "count": point.count,
                            "sum": point.sum or 0.0,
                        }
                    else:
                        entry["series"][key] = point.value
    return result


def _counter_delta(reader, name: str, attrs: dict) -> int:
    key = frozenset(attrs.items())
    current = _snapshot(reader).get(name, {}).get("series", {}).get(key)
    baseline = _clean_collectors.baseline.get(name, {}).get("series", {}).get(key)
    after = current if current is not None else 0
    before = baseline if baseline is not None else 0
    return after - before


def _hist_delta(reader, name: str, attrs: dict) -> dict:
    key = frozenset(attrs.items())
    current = _snapshot(reader).get(name, {}).get("series", {}).get(key, {})
    baseline = _clean_collectors.baseline.get(name, {}).get("series", {}).get(key, {})
    return {
        "count": (current.get("count") or 0) - (baseline.get("count") or 0),
        "sum": (current.get("sum") or 0.0) - (baseline.get("sum") or 0.0),
    }


def _spans(span_exporter) -> list:
    return span_exporter.get_finished_spans()


def _last_span(span_exporter):
    spans = _spans(span_exporter)
    assert spans, "expected at least one finished span"
    return spans[-1]


# ---------------------------------------------------------------------------
# 错误枚举映射
# ---------------------------------------------------------------------------


def test_error_type_enum_matches_spec_exactly():
    assert ts.ERROR_TYPES == frozenset(
        {
            "unsupported_file_type",
            "file_too_large",
            "embedding_failed",
            "vector_index_failed",
            "sqlite_failed",
            "timeout",
            "other",
        }
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("unsupported_file_type", "unsupported_file_type"),
        ("file_too_large", "file_too_large"),
        ("embedding_provider_failed", "embedding_failed"),
        ("embedding_vector_mismatch", "embedding_failed"),
        ("qdrant_client_not_installed", "vector_index_failed"),
        ("document_not_found", "other"),
        ("index_job_not_found", "other"),
        ("invalid_tags", "other"),
        ("infrastructure_error", "other"),
        ("validation_error", "other"),
        ("totally_unknown_code", "other"),
        ("embedding failed: secret payload text", "other"),
        ("", "other"),
        (123, "other"),
        (None, "other"),
    ],
)
def test_map_error_type(raw, expected):
    assert ts.map_error_type(raw) == expected


def test_error_type_for_exception_maps_stable_codes():
    assert ts.error_type_for_exception(ValueError("embedding_provider_failed")) == "embedding_failed"
    assert ts.error_type_for_exception(RuntimeError("embedding_vector_mismatch")) == "embedding_failed"
    assert ts.error_type_for_exception(ValueError("document_not_found")) == "other"


def test_error_type_for_exception_sqlite_and_unknown_text():
    import sqlite3

    assert ts.error_type_for_exception(sqlite3.OperationalError("SELECT secret")) == "sqlite_failed"
    assert ts.error_type_for_exception(RuntimeError("raw internal message")) == "other"


def test_error_type_for_exception_cancelled_is_outcome_not_error():
    assert ts.error_type_for_exception(asyncio.CancelledError()) is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (None, "success"),
        (asyncio.CancelledError(), "cancelled"),
        (ValueError("boom"), "failure"),
        (RuntimeError("embedding_provider_failed"), "failure"),
    ],
)
def test_outcome_for_exception(exc, expected):
    assert ts.outcome_for_exception(exc) == expected


# ---------------------------------------------------------------------------
# retrieval_attrs：结果数 + 过滤条件类型与数量（不带值）
# ---------------------------------------------------------------------------


class _Filter:
    """模拟 RetrievalFilter 的 duck-typed 输入（模块禁止 import app.domain）。"""

    def __init__(self, tags=None, source_kind=None, document_status=None, min_score=None):
        self.tags = tags if tags is not None else {}
        self.source_kind = source_kind
        self.document_status = document_status
        self.min_score = min_score


def test_retrieval_attrs_result_count_only():
    assert ts.retrieval_attrs(7) == {"nkb.retrieval.result_count": 7}
    assert ts.retrieval_attrs(None) == {}


def test_retrieval_attrs_filter_counts_without_values():
    attrs = ts.retrieval_attrs(
        3,
        _Filter(tags={"a": "secret-value", "b": "other-tag"}, source_kind="upload", min_score=0.42),
    )
    assert attrs == {
        "nkb.retrieval.result_count": 3,
        "nkb.retrieval.filter_tags": 2,
        "nkb.retrieval.filter_source_kind": 1,
        "nkb.retrieval.filter_min_score": 1,
    }
    dumped = json.dumps(attrs)
    assert "secret-value" not in dumped
    assert "upload" not in dumped
    assert "0.42" not in dumped


def test_retrieval_attrs_accepts_mapping_and_drops_unknown_keys():
    attrs = ts.retrieval_attrs(1, {"tags": {"k": "v"}, "intruder": 3})
    assert attrs == {"nkb.retrieval.result_count": 1, "nkb.retrieval.filter_tags": 1}


# ---------------------------------------------------------------------------
# span helpers
# ---------------------------------------------------------------------------


def test_retrieval_span_success_is_internal_and_current(span_exporter):
    with ts.retrieval_span() as span:
        assert trace.get_current_span() is span
        assert span.is_recording()
    assert trace.get_current_span() is not span
    recorded = _last_span(span_exporter)
    assert recorded.name == "retrieval.search"
    assert recorded.kind == trace.SpanKind.INTERNAL
    assert recorded.attributes.get("nkb.retrieval.outcome") == "success"
    assert recorded.end_time is not None


def test_retrieval_span_error_marks_safe_error_type_without_events(span_exporter):
    with pytest.raises(ValueError, match="embedding_provider_failed"):
        with ts.retrieval_span():
            raise ValueError("embedding_provider_failed")
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("error.type") == "embedding_failed"
    assert recorded.attributes.get("nkb.retrieval.outcome") == "failure"
    assert recorded.status.status_code == trace.StatusCode.ERROR
    assert len(recorded.events) == 1
    assert recorded.events[0].name.startswith('nkb.')
    assert all(event.name != 'exception' for event in recorded.events)


def test_retrieval_span_error_does_not_leak_raw_message(span_exporter):
    secret = "raw failure containing https://user:pass@host/db?q=secret"
    with pytest.raises(RuntimeError):
        with ts.retrieval_span():
            raise RuntimeError(secret)
    recorded = _last_span(span_exporter)
    dumped = json.dumps(recorded.attributes, default=str)
    assert secret not in dumped
    assert "user:pass" not in dumped
    assert len(recorded.events) == 1
    assert recorded.events[0].name.startswith('nkb.')
    assert all(event.name != 'exception' for event in recorded.events)


def test_retrieval_span_cancelled_is_outcome_without_error_type(span_exporter):
    with pytest.raises(asyncio.CancelledError):
        with ts.retrieval_span():
            raise asyncio.CancelledError()
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.retrieval.outcome") == "cancelled"
    assert "error.type" not in recorded.attributes
    assert recorded.status.status_code != trace.StatusCode.ERROR
    assert len(recorded.events) == 1
    assert recorded.events[0].name.startswith('nkb.')
    assert all(event.name != 'exception' for event in recorded.events)


def test_index_run_span_records_document_id(span_exporter):
    with ts.index_run_span("doc-123"):
        pass
    recorded = _last_span(span_exporter)
    assert recorded.name == "index.run"
    assert recorded.attributes.get("nkb.index.document_id") == "doc-123"
    assert recorded.attributes.get("nkb.index.outcome") == "success"


def test_index_run_span_without_document_id_omits_attribute(span_exporter):
    with ts.index_run_span():
        pass
    recorded = _last_span(span_exporter)
    assert "nkb.index.document_id" not in recorded.attributes


@pytest.mark.parametrize(
    ("stage", "expected_name"),
    [
        ("parse", "index.parse"),
        ("split", "index.split"),
        ("embed", "index.embed"),
        ("write", "index.write"),

    ],
)
def test_index_stage_span_names(span_exporter, stage, expected_name):
    with ts.index_stage_span(stage):
        pass
    recorded = _last_span(span_exporter)
    assert recorded.name == expected_name
    assert recorded.kind == trace.SpanKind.INTERNAL
    assert recorded.attributes.get("nkb.index.outcome") == "success"


def test_index_stage_span_failure_outcome(span_exporter):
    with pytest.raises(RuntimeError):
        with ts.index_stage_span("write"):
            raise RuntimeError("embedding_provider_failed")
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.index.outcome") == "failure"
    assert recorded.attributes.get("error.type") == "embedding_failed"


def test_set_index_chunk_count_attribute(span_exporter):
    with ts.index_run_span("doc-1") as span:
        ts.set_index_chunk_count(span, 12)
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.index.chunk_count") == 12


def test_span_helper_isolated_from_tracer_failure(monkeypatch):
    def broken_tracer(*args, **kwargs):
        raise RuntimeError("tracer exploded")

    monkeypatch.setattr(ts.trace, "get_tracer", broken_tracer)
    with ts.retrieval_span() as span:
        assert span is not None
    with ts.index_run_span("doc-2"):
        pass
    with ts.embedding_observer("bge-m3") as scope:
        scope.set_input_tokens(3)


# ---------------------------------------------------------------------------
# metric record helpers
# ---------------------------------------------------------------------------


def test_record_retrieval_success_records_all_three(reader):
    with ts.retrieval_span():
        ts.record_retrieval("success", result_count=3, seconds=0.5)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"nkb.retrieval.outcome": "success"}) == 1
    results = _hist_delta(reader, "nkb.retrieval.results", {})
    assert results["count"] == 1
    assert results["sum"] == 3
    duration = _hist_delta(reader, "nkb.retrieval.duration", {"nkb.retrieval.outcome": "success"})
    assert duration["count"] == 1
    assert duration["sum"] >= 0


def test_record_retrieval_error_records_requests_and_duration_not_results(reader):
    with ts.retrieval_span():
        ts.record_retrieval("failure", result_count=9, seconds=0.2)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"nkb.retrieval.outcome": "failure"}) == 1
    assert _hist_delta(reader, "nkb.retrieval.results", {})["count"] == 0
    duration = _hist_delta(reader, "nkb.retrieval.duration", {"nkb.retrieval.outcome": "failure"})
    assert duration["count"] == 1
    assert duration["sum"] >= 0


def test_record_retrieval_cancelled_outcome(reader):
    with ts.retrieval_span():
        ts.record_retrieval("cancelled", seconds=0.1)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"nkb.retrieval.outcome": "cancelled"}) == 1
    assert _hist_delta(reader, "nkb.retrieval.results", {})["count"] == 0
    assert _hist_delta(reader, "nkb.retrieval.duration", {"nkb.retrieval.outcome": "cancelled"})["count"] == 1


def test_record_index_success_records_jobs_and_chunks(reader):
    with ts.index_run_span():
        ts.record_index("success", chunk_count=12)
    assert _counter_delta(reader, "nkb.index.jobs", {"nkb.index.outcome": "success"}) == 1
    chunks = _hist_delta(reader, "nkb.index.chunks", {})
    assert chunks["count"] == 1
    assert chunks["sum"] == 12


def test_record_index_error_and_cancelled_skip_chunks(reader):
    with ts.index_run_span():
        ts.record_index("failure", chunk_count=5)
    with ts.index_run_span():
        ts.record_index("cancelled")
    assert _counter_delta(reader, "nkb.index.jobs", {"nkb.index.outcome": "failure"}) == 1
    assert _counter_delta(reader, "nkb.index.jobs", {"nkb.index.outcome": "cancelled"}) == 1
    assert _hist_delta(reader, "nkb.index.chunks", {})["count"] == 0


def test_record_helpers_isolated_from_meter_failure(monkeypatch):
    def broken_meter(*args, **kwargs):
        raise RuntimeError("meter exploded")

    monkeypatch.setattr(ts.metrics, "get_meter", broken_meter)
    with ts.retrieval_span():
        ts.record_retrieval("success", result_count=1, seconds=0.1)
    with ts.index_run_span():
        ts.record_index("success", chunk_count=1)


# ---------------------------------------------------------------------------
# 七个 instrument 的名称与单位
# ---------------------------------------------------------------------------


def test_business_instruments_exact_names_and_units(reader):
    with ts.embedding_observer("unit-check-model") as scope:
        scope.set_input_tokens(4)
    ts.record_retrieval("success", result_count=1, seconds=0.01)
    ts.record_index("success", chunk_count=1)

    with ts.operation_span("document.upload"):
        pass
    with ts.operation_span("mcp.status"):
        pass
    with ts.index_run_span(), ts.index_stage_span("parse"):
        pass
    snapshot = _snapshot(reader)
    expected = {
        "gen_ai.client.operation.duration": "s",
        "gen_ai.client.token.usage": "{token}",
        "nkb.index.duration": "s",
        "nkb.index.stage.duration": "s",
        "nkb.index.jobs": "1",
        "nkb.index.chunks": "{chunk}",
        "nkb.retrieval.requests": "1",
        "nkb.retrieval.results": "{result}",
        "nkb.retrieval.duration": "s",
        "nkb.document.uploads": "1",
        "nkb.document.upload.duration": "s",
        "nkb.mcp.requests": "1",
        "nkb.mcp.duration": "s",
    }
    for name, unit in expected.items():
        assert name in snapshot, f"missing instrument {name}"
        assert snapshot[name]["unit"] == unit, f"unit mismatch for {name}"
    observed = {name for name in snapshot if name.startswith(("nkb.", "gen_ai."))}
    assert observed == set(expected)


def test_no_record_retrieval_duration_helper_exists():
    assert not hasattr(ts, "record_retrieval_duration")


# ---------------------------------------------------------------------------
# embedding 操作上下文工厂
# ---------------------------------------------------------------------------


def test_embedding_observer_creates_genai_client_span(span_exporter):
    with ts.embedding_observer("bge-m3") as scope:
        scope.set_input_tokens(7)
    recorded = _last_span(span_exporter)
    assert recorded.name == "embeddings bge-m3"
    assert recorded.kind == trace.SpanKind.CLIENT
    assert recorded.attributes.get("gen_ai.operation.name") == "embeddings"
    assert recorded.attributes.get("gen_ai.request.model") == "bge-m3"
    assert recorded.attributes.get("gen_ai.usage.input_tokens") == 7
    assert recorded.attributes.get("nkb.model.outcome") == "success"
    assert len(recorded.events) == 1
    assert recorded.events[0].name.startswith('nkb.')
    assert all(event.name != 'exception' for event in recorded.events)


def test_embedding_observer_records_duration_and_token_metrics(reader):
    with ts.embedding_observer("metric-model") as scope:
        scope.set_input_tokens(7)
    duration = _hist_delta(
        reader,
        "gen_ai.client.operation.duration",
        {"gen_ai.operation.name": "embeddings", "gen_ai.request.model": "metric-model"},
    )
    assert duration["count"] == 1
    assert duration["sum"] > 0
    token = _hist_delta(
        reader,
        "gen_ai.client.token.usage",
        {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": "metric-model",
            "gen_ai.token.type": "input",
        },
    )
    assert token["count"] == 1
    assert token["sum"] == 7


def test_embedding_observer_missing_tokens_records_duration_only(reader):
    with ts.embedding_observer("no-token-model"):
        pass
    duration = _hist_delta(
        reader,
        "gen_ai.client.operation.duration",
        {"gen_ai.operation.name": "embeddings", "gen_ai.request.model": "no-token-model"},
    )
    assert duration["count"] == 1
    assert _hist_delta(reader, "gen_ai.client.token.usage", {})["count"] == 0


@pytest.mark.parametrize("invalid", [None, -3, "7", True, 1.5])
def test_embedding_scope_rejects_invalid_tokens(reader, invalid):
    with ts.embedding_observer("invalid-token-model") as scope:
        scope.set_input_tokens(invalid)
    assert _hist_delta(reader, "gen_ai.client.token.usage", {})["count"] == 0


def test_embedding_observer_error_records_metrics_and_propagates(reader, span_exporter):
    with pytest.raises(ValueError, match="embedding_provider_failed"):
        with ts.embedding_observer("error-model") as scope:
            scope.set_input_tokens(5)
            raise ValueError("embedding_provider_failed")
    duration = _hist_delta(
        reader,
        "gen_ai.client.operation.duration",
        {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": "error-model",
            "error.type": "embedding_failed",
        },
    )
    assert duration["count"] == 1
    token = _hist_delta(
        reader,
        "gen_ai.client.token.usage",
        {
            "gen_ai.operation.name": "embeddings",
            "gen_ai.request.model": "error-model",
            "gen_ai.token.type": "input",
        },
    )
    assert token["count"] == 1
    assert token["sum"] == 5
    recorded = _last_span(span_exporter)
    assert recorded.status.status_code == trace.StatusCode.ERROR
    assert recorded.attributes.get("nkb.model.outcome") == "failure"
    assert len(recorded.events) == 1
    assert recorded.events[0].name.startswith('nkb.')
    assert all(event.name != 'exception' for event in recorded.events)


def test_embedding_observer_cancelled_records_duration_without_error_type(reader, span_exporter):
    with pytest.raises(asyncio.CancelledError):
        with ts.embedding_observer("cancel-model"):
            raise asyncio.CancelledError()
    snapshot = _snapshot(reader)
    duration_series = snapshot["gen_ai.client.operation.duration"]["series"]
    cancel_keys = [
        key
        for key in duration_series
        if dict(key).get("gen_ai.request.model") == "cancel-model"
    ]
    assert len(cancel_keys) == 1
    assert "error.type" not in dict(cancel_keys[0])
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.model.outcome") == "cancelled"
    assert "error.type" not in recorded.attributes


def test_embedding_observer_isolated_from_meter_failure(reader, monkeypatch):
    def broken_meter(*args, **kwargs):
        raise RuntimeError("meter exploded")

    monkeypatch.setattr(ts.metrics, "get_meter", broken_meter)
    with ts.embedding_observer("broken-meter-model") as scope:
        scope.set_input_tokens(2)
    with pytest.raises(RuntimeError, match="business failure still propagates"):
        with ts.embedding_observer("raising-model"):
            raise RuntimeError("business failure still propagates")


# ---------------------------------------------------------------------------
# ContextVar 操作上下文
# ---------------------------------------------------------------------------


def test_operation_context_nested_restore():
    with ts.operation_context(**{"nkb.request.id":"outer", "nkb.retrieval.outcome":"failure"}):
        parent=dict(ts.current_operation_context())
        with ts.operation_context(**{"nkb.index.stage":"embed"}):
            current=ts.current_operation_context()
            assert current["nkb.request.id"] == "outer"
            assert current["nkb.index.stage"] == "embed"
            assert "nkb.retrieval.outcome" not in current
        assert ts.current_operation_context() == parent
    assert ts.current_operation_context() == {}


def test_operation_context_reset_on_exception_and_cancel():
    with pytest.raises(ValueError):
        with ts.operation_context(operation="a"):
            raise ValueError("boom")
    assert ts.current_operation_context() == {}
    with pytest.raises(asyncio.CancelledError):
        with ts.operation_context(operation="b"):
            raise asyncio.CancelledError()
    assert ts.current_operation_context() == {}


def test_operation_context_concurrent_tasks_isolated():
    results = {}

    async def worker(name: str) -> None:
        with ts.operation_context(**{"nkb.request.id":name}):
            await asyncio.sleep(0.01)
            results[name] = ts.current_operation_context()["nkb.request.id"]

    async def main() -> None:
        await asyncio.gather(worker("task-a"), worker("task-b"))

    asyncio.run(main())
    assert results == {"task-a": "task-a", "task-b": "task-b"}


def test_span_helpers_update_operation_context(span_exporter):
    with ts.retrieval_span():
        assert ts.current_operation_context()["nkb.retrieval.operation"] == "search"
        with ts.index_stage_span("embed"):
            assert ts.current_operation_context()["nkb.index.stage"] == "embed"
        assert "nkb.index.stage" not in ts.current_operation_context()
    assert ts.current_operation_context() == {}
    with ts.embedding_observer("ctx-model"):
        assert ts.current_operation_context()["gen_ai.operation.name"] == "embeddings"


# ---------------------------------------------------------------------------
# 模块约束守护
# ---------------------------------------------------------------------------


def test_module_imports_only_stdlib_and_otel_api():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    forbidden_prefixes = (
        "opentelemetry.sdk",
        "opentelemetry.exporter",
        "opentelemetry.instrumentation",
        "app.infrastructure",
        "app.domain",
        "app.config",
    )
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if name.startswith(forbidden_prefixes):
                violations.append(name)
    assert violations == []


def test_module_uses_start_span_and_use_span_without_autorecord():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "start_span(" in source
    assert "trace.use_span(" in source
    assert "record_exception=False" in source
    assert "set_status_on_exception=False" in source
    assert "start_as_current_span" not in source


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancelled'])
def test_completed_operation_records_once_and_preserves_exception(reader, outcome):
    before = _snapshot(reader)
    error = asyncio.CancelledError() if outcome == 'cancelled' else ValueError('private')
    try:
        with ts.completed_operation('document.upload'):
            if outcome != 'success':
                raise error
    except BaseException as caught:
        assert caught is error
    state = _snapshot(reader)
    key = frozenset({'nkb.document.outcome':outcome}.items())
    assert state['nkb.document.uploads']['series'][key] - before.get('nkb.document.uploads', {}).get('series', {}).get(key, 0) == 1
    assert 'private' not in str(state)


def test_zero_result_bucket_is_enforced_by_runtime_view():
    from app.infrastructure.observability.setup import _metric_views
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    local_reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[local_reader], views=_metric_views(ts))
    histogram = provider.get_meter('isolated').create_histogram(ts.INSTRUMENT_RETRIEVAL_RESULTS, unit='{result}')
    histogram.record(0)
    histogram.record(1)
    data = local_reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0]
    assert data.explicit_bounds[0] == 0
    assert data.bucket_counts[0] == 1
    provider.shutdown()


# ---------------------------------------------------------------------------
# Langfuse 映射契约锁定：三类操作的精确 nkb.content.* 来源
# ---------------------------------------------------------------------------

LANGFUSE_CONTENT_SOURCES = (
    "index_input",
    "index_result",
    "retrieval_input",
    "retrieval_output",
    "embedding_input",
)


@pytest.fixture
def bounded_details():
    """激活注入式脱敏通道（与 composition 相同入口），结束后恢复默认禁用。"""
    from app.application import telemetry_details as td

    td.configure_details(lambda text: text, max_bytes=1024, max_total_bytes=4096)
    yield td
    td.configure_details(None)


def _assert_content_metadata_types(attributes, field):
    """四项元数据：存在即类型正确；缺失不补造（不强制全部存在）。"""
    for suffix, kind in (
        ("original_bytes", int),
        ("truncated", bool),
        ("incomplete", bool),
        ("observed_bytes", int),
    ):
        key = f"nkb.content.{field}_{suffix}"
        if key not in attributes:
            continue
        value = attributes[key]
        assert type(value) is kind, f"{key} must be {kind.__name__}, got {type(value)}"
        if kind is int:
            assert value >= 0


def _assert_complete_envelope(attributes, field):
    content = attributes[f"nkb.content.{field}"]
    assert type(content) is str
    assert 0 < len(content.encode("utf-8")) <= 1024
    assert attributes[f"nkb.content.{field}_original_bytes"] > 0
    assert type(attributes[f"nkb.content.{field}_truncated"]) is bool
    _assert_content_metadata_types(attributes, field)


def test_content_source_vocabulary_matches_langfuse_mapping_contract():
    from app.application import telemetry_contract as contract

    for field in LANGFUSE_CONTENT_SOURCES:
        key = f"nkb.content.{field}"
        assert contract.ATTRIBUTES.get(key) == "text"
        assert contract.valid_attribute(key, "bounded safe text")
    # 应用侧既有 OTel 合同保留 embedding_result（仅 Collector 的 Langfuse
    # 分支不导出）；锁定应用出口未被 Langfuse 白名单反向收窄。
    assert contract.ATTRIBUTES.get("nkb.content.embedding_result") == "text"
    for field in LANGFUSE_CONTENT_SOURCES:
        for suffix, kind in contract.CONTENT_METADATA.items():
            assert contract.ATTRIBUTES.get(f"nkb.content.{field}_{suffix}") == kind


def test_index_run_span_carries_bounded_content_sources(bounded_details, span_exporter):
    with ts.index_run_span("doc-42"):
        bounded_details.update_current_details(index_input={"document_id": "doc-42"})
        bounded_details.update_current_details(
            index_result={"document_id": "doc-42", "chunk_count": 3, "stages": ["parse", "split"]}
        )
    recorded = _last_span(span_exporter)
    assert recorded.name == "index.run"
    assert json.loads(recorded.attributes["nkb.content.index_input"]) == {"document_id": "doc-42"}
    result_payload = json.loads(recorded.attributes["nkb.content.index_result"])
    assert result_payload["document_id"] == "doc-42"
    assert result_payload["chunk_count"] == 3
    for field in ("index_input", "index_result"):
        _assert_complete_envelope(recorded.attributes, field)
    assert len(recorded.events) == 1
    event = recorded.events[0]
    assert event.name == "nkb.index.completed"
    assert event.attributes["nkb.content.index_input"] == recorded.attributes["nkb.content.index_input"]
    assert event.attributes["nkb.content.index_result"] == recorded.attributes["nkb.content.index_result"]


def test_retrieval_search_span_carries_bounded_content_sources(bounded_details, span_exporter):
    with ts.retrieval_span():
        bounded_details.update_current_details(
            retrieval_input={"query": "kb contract", "top_k": 2}
        )
        bounded_details.update_current_details(
            retrieval_output={
                "result_count": 1,
                "returned_count": 1,
                "results": [{"document_id": "doc-1", "snippet": "safe"}],
            }
        )
    recorded = _last_span(span_exporter)
    assert recorded.name == "retrieval.search"
    assert json.loads(recorded.attributes["nkb.content.retrieval_input"])["query"] == "kb contract"
    output_payload = json.loads(recorded.attributes["nkb.content.retrieval_output"])
    assert output_payload["result_count"] == 1
    assert output_payload["results"][0]["document_id"] == "doc-1"
    for field in ("retrieval_input", "retrieval_output"):
        _assert_complete_envelope(recorded.attributes, field)
    assert len(recorded.events) == 1
    event = recorded.events[0]
    assert event.name == "nkb.retrieval.completed"
    assert event.attributes["nkb.content.retrieval_input"] == recorded.attributes["nkb.content.retrieval_input"]
    assert event.attributes["nkb.content.retrieval_output"] == recorded.attributes["nkb.content.retrieval_output"]


def test_embeddings_span_carries_bounded_embedding_input_without_synthesized_result(
    bounded_details, span_exporter
):
    with ts.embedding_observer("contract-model") as scope:
        scope.set_input(["safe one", "safe two"])
        scope.set_input_tokens(5)
    recorded = _last_span(span_exporter)
    assert recorded.name == "embeddings contract-model"
    payload = json.loads(recorded.attributes["nkb.content.embedding_input"])
    assert payload["model"] == "contract-model"
    assert payload["input_count"] == 2
    assert payload["prefixes_truncated"] is False
    assert all(
        type(prefix) is str and len(prefix.encode("utf-8")) <= 256
        for prefix in payload["text_prefixes"]
    )
    _assert_content_metadata_types(recorded.attributes, "embedding_input")
    # 未报告结果时不合成业务 output（应用层不发布 embedding_result）。
    assert "nkb.content.embedding_result" not in recorded.attributes


def test_embedding_result_outlet_remains_published_at_app_layer(bounded_details, span_exporter):
    with ts.embedding_observer("result-model") as scope:
        scope.set_input(["safe"])
        scope.set_result(1, 1024)
    recorded = _last_span(span_exporter)
    result_payload = json.loads(recorded.attributes["nkb.content.embedding_result"])
    assert result_payload["model"] == "result-model"
    assert result_payload["output_count"] == 1
    assert result_payload["dimensions"] == 1024


def test_missing_content_directions_are_omitted_independently(bounded_details, span_exporter):
    with ts.index_run_span("doc-7"):
        bounded_details.update_current_details(index_input={"document_id": "doc-7"})
    recorded = _last_span(span_exporter)
    assert "nkb.content.index_input" in recorded.attributes
    assert "nkb.content.index_result" not in recorded.attributes
    with ts.retrieval_span():
        pass
    recorded = _last_span(span_exporter)
    assert not [key for key in recorded.attributes if key.startswith("nkb.content.")]


def test_failure_and_cancel_do_not_synthesize_successful_outputs(bounded_details, span_exporter):
    with pytest.raises(ValueError, match="embedding_provider_failed"):
        with ts.retrieval_span():
            bounded_details.update_current_details(retrieval_input={"query": "will fail"})
            raise ValueError("embedding_provider_failed")
    recorded = _last_span(span_exporter)
    assert recorded.attributes["nkb.retrieval.outcome"] == "failure"
    assert "nkb.content.retrieval_input" in recorded.attributes
    assert "nkb.content.retrieval_output" not in recorded.attributes
    # error 详情仅含固定结果与枚举类型，不含异常正文。
    error_payload = json.loads(recorded.attributes["nkb.content.error"])
    assert error_payload == {"outcome": "failure", "type": "embedding_failed"}

    with pytest.raises(asyncio.CancelledError):
        with ts.index_run_span("doc-9"):
            bounded_details.update_current_details(index_input={"document_id": "doc-9"})
            raise asyncio.CancelledError()
    recorded = _last_span(span_exporter)
    assert recorded.attributes["nkb.index.outcome"] == "cancelled"
    assert "error.type" not in recorded.attributes
    assert "nkb.content.index_input" in recorded.attributes
    assert "nkb.content.index_result" not in recorded.attributes


def test_no_observation_or_langfuse_application_attributes(bounded_details, span_exporter):
    with ts.index_run_span("doc-3"):
        bounded_details.update_current_details(index_input={"document_id": "doc-3"})
        bounded_details.update_current_details(index_result={"chunk_count": 1})
    with ts.retrieval_span():
        bounded_details.update_current_details(retrieval_input={"query": "q"})
    with ts.embedding_observer("guard-model") as scope:
        scope.set_input(["safe"])
        scope.set_input_tokens(1)
    assert _spans(span_exporter), "expected spans for all three operations"
    for recorded in _spans(span_exporter):
        keys = list(recorded.attributes)
        for event in recorded.events:
            keys.extend(event.attributes)
        for key in keys:
            assert not key.startswith("observation.")
            assert not key.startswith("langfuse.")


def test_completion_scope_batch_publishes_frozen_attributes_once(bounded_details, span_exporter):
    entered = []

    class _RecordingScope:
        def __init__(self, attributes):
            self._attributes = dict(attributes)

        def __enter__(self):
            entered.append(self._attributes)
            return self

        def __exit__(self, *exc):
            return None

    ts.configure_completion(
        callback=None, scope_factory=lambda attributes: _RecordingScope(attributes)
    )
    try:
        with ts.retrieval_span():
            bounded_details.update_current_details(retrieval_input={"query": "scope"})
    finally:
        ts.configure_completion()
    recorded = _last_span(span_exporter)
    assert len(recorded.events) == 1
    assert len(entered) == 1
    assert (
        entered[0]["nkb.content.retrieval_input"]
        == recorded.attributes["nkb.content.retrieval_input"]
    )


# ---------------------------------------------------------------------------
# T6: embeddings 成本事实与三个成本 Counter
# ---------------------------------------------------------------------------


@pytest.fixture
def cost_calculator():
    """保存/恢复模块级计价闭包，避免跨测试泄漏。"""
    saved = ts._cost_calculator
    yield
    ts._cost_calculator = saved


def _estimated_result(amount="0.00002", billing_model="cost-model", **overrides):
    from decimal import Decimal

    result = {
        "status": "estimated",
        "amount_usd": float(Decimal(amount)),
        "cost_decimal": amount,
        "components": {"input": float(Decimal(amount))},
        "pricing_version": "v-test@1",
        "price_id": "p1",
        "reason": None,
        "currency": "USD",
        "billing_provider": "ollama",
        "billing_model": billing_model,
    }
    result.update(overrides)
    return result


_COST_DIMS = {
    "gen_ai.operation.name": "embeddings",
    "nkb.model.billing_provider": "ollama",
}


def test_set_input_tokens_distinguishes_known_invalid_missing():
    with ts.embedding_observer("state-model") as scope:
        assert scope._input_state == "unknown"
        scope.set_input_tokens(7)
        assert scope._input_state == "known"
        scope.set_input_tokens(-3)
        assert scope._input_state == "invalid"
        scope.set_input_tokens("7")
        assert scope._input_state == "invalid"
        scope.set_input_tokens(True)
        assert scope._input_state == "invalid"
        scope.set_input_tokens(None)
        assert scope._input_state == "unknown"


def test_embedding_cost_estimated_records_facts_and_counters(
    reader, span_exporter, cost_calculator
):
    ts._cost_calculator = lambda facts: _estimated_result()
    with ts.embedding_observer("cost-model") as scope:
        scope.set_input_tokens(200)
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "estimated"
    assert span.attributes["nkb.model.cost_usd"] == 0.00002
    assert span.attributes["nkb.model.cost_input_usd"] == 0.00002
    assert span.attributes["nkb.model.cost_decimal"] == "0.00002"
    assert span.attributes["nkb.model.cost_currency"] == "USD"
    assert span.attributes["nkb.model.pricing_version"] == "v-test@1"
    assert span.attributes["nkb.model.price_id"] == "p1"
    assert span.attributes["nkb.model.billing_provider"] == "ollama"
    assert span.attributes["nkb.model.billing_model"] == "cost-model"
    assert span.attributes["nkb.model.usage_known"] is True
    assert span.attributes["gen_ai.usage.input_tokens"] == 200
    event = span.events[0]
    assert event.name == "nkb.model.completed"
    assert event.attributes["nkb.model.cost_status"] == "estimated"
    assert event.attributes["nkb.model.cost_usd"] == 0.00002
    dims = {**_COST_DIMS, "nkb.model.billing_model": "cost-model"}
    assert _counter_delta(reader, "nkb.model.cost", dims) == 0.00002
    assert (
        _counter_delta(
            reader, "nkb.model.cost_components", {**dims, "nkb.model.cost_component": "input"}
        )
        == 0.00002
    )
    assert (
        _counter_delta(
            reader,
            "nkb.model.cost_attempts",
            {**dims, "nkb.model.cost_status": "estimated", "nkb.model.cost_reason": "none"},
        )
        == 1
    )
    # unknown 出口不产生任何金额
    assert _counter_delta(reader, "nkb.model.cost", {**dims, "nkb.model.billing_model": "absent-model"}) == 0


def test_cost_counter_model_label_outside_whitelist_is_other(
    reader, span_exporter, cost_calculator
):
    """三个成本 Counter 的模型维度必须走 map_model_name 白名单（未通过
    model 校验 -> other），任意计费模型名不得直接成为指标标签；
    Span/metadata 的计费身份投影（map_billing_model）保留真实名称。"""
    result = _estimated_result(billing_model="bad model!")
    ts._cost_calculator = lambda facts: result
    with ts.embedding_observer("bad model!") as scope:
        scope.set_input_tokens(200)
    dims = {**_COST_DIMS, "nkb.model.billing_model": "other"}
    assert _counter_delta(reader, "nkb.model.cost", dims) == 0.00002
    assert (
        _counter_delta(
            reader,
            "nkb.model.cost_attempts",
            {**dims, "nkb.model.cost_status": "estimated", "nkb.model.cost_reason": "none"},
        )
        == 1
    )
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.billing_model"] == "bad model!"


def test_embedding_cost_explicit_zero_still_adds_counter(
    reader, span_exporter, cost_calculator
):
    ts._cost_calculator = lambda facts: _estimated_result(
        amount="0", billing_model="zero-model"
    )
    with ts.embedding_observer("zero-model") as scope:
        scope.set_input_tokens(0)
    dims = {**_COST_DIMS, "nkb.model.billing_model": "zero-model"}
    key = frozenset(dims.items())
    baseline = _clean_collectors.baseline.get("nkb.model.cost", {}).get("series", {})
    after = _snapshot(reader).get("nkb.model.cost", {}).get("series", {})
    # 显式零价：estimated 也要 add(0)，序列必须存在（区别于从未记录）
    assert key not in baseline
    assert key in after and after[key] == 0.0


def test_embedding_cost_unknown_records_status_reason_without_amounts(
    reader, span_exporter, cost_calculator
):
    ts._cost_calculator = lambda facts: {
        "status": "unknown",
        "reason": "price_not_found",
        "currency": "USD",
        "pricing_version": "v-test@1",
        "billing_provider": "ollama",
        "billing_model": "unknown-model",
        "amount_usd": 0.0,
        "price_id": None,
        "cost_decimal": None,
        "components": None,
    }
    with ts.embedding_observer("unknown-model") as scope:
        scope.set_input_tokens(5)
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "unknown"
    assert span.attributes["nkb.model.cost_reason"] == "price_not_found"
    assert span.attributes["nkb.model.pricing_version"] == "v-test@1"
    assert span.attributes["nkb.model.billing_provider"] == "ollama"
    for key in (
        "nkb.model.cost_usd",
        "nkb.model.cost_decimal",
        "nkb.model.cost_input_usd",
        "nkb.model.price_id",
    ):
        assert key not in span.attributes
    dims = {**_COST_DIMS, "nkb.model.billing_model": "unknown-model"}
    assert (
        _counter_delta(
            reader,
            "nkb.model.cost_attempts",
            {**dims, "nkb.model.cost_status": "unknown", "nkb.model.cost_reason": "price_not_found"},
        )
        == 1
    )
    assert _counter_delta(reader, "nkb.model.cost", dims) == 0
    assert _counter_delta(reader, "nkb.model.cost_components", dims) == 0


def test_embedding_cost_unknown_retains_price_id_only_when_resolved(
    reader, span_exporter, cost_calculator
):
    # 唯一命中价格项后的 unknown 保留 price_id 供追溯；未定位价格项的
    # unknown 不携带（price_id 不是自动推价材料——unknown 不投影 model.name）
    ts._cost_calculator = lambda facts: {
        "status": "unknown",
        "reason": "amount_unrepresentable",
        "currency": "USD",
        "pricing_version": "v-test@1",
        "billing_provider": "ollama",
        "billing_model": "ttl-model",
        "amount_usd": 0.0,
        "price_id": "p-ollama-ttl",
        "cost_decimal": None,
        "components": None,
    }
    with ts.embedding_observer("ttl-model") as scope:
        scope.set_input_tokens(5)
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "unknown"
    assert span.attributes["nkb.model.cost_reason"] == "amount_unrepresentable"
    assert span.attributes["nkb.model.pricing_version"] == "v-test@1"
    assert span.attributes["nkb.model.price_id"] == "p-ollama-ttl"
    assert "nkb.model.cost_usd" not in span.attributes
    assert "nkb.model.cost_decimal" not in span.attributes
    assert "nkb.model.name" not in span.attributes


# ---------------------------------------------------------------------------
# 真实组合根接线（用户评审修复 第五轮-nkb）：计价闭包经
# composition.make_embedding_cost_calculator 按生产方式装配，不伪造结果 dict
# ---------------------------------------------------------------------------


def _configure_catalog_calculator(tmp_path, unit_price):
    """写入临时定价目录并按生产方式装配组合根计价闭包。"""
    import hashlib

    from app.composition import make_embedding_cost_calculator
    from app.config import Settings
    from app.infrastructure.embedding.pricing_catalog import jcs_canonicalize

    catalog = {
        "schema_version": 1,
        "content_version": "test-cost-fix-v1",
        "currency": "USD",
        "prices": [
            {
                "price_id": "p-tiny",
                "billing_provider": "ollama",
                "billing_model": "bge-m3",
                "operation": "embeddings",
                "aliases": [],
                "unit_prices": {"input": unit_price},
                "source_url": "test://pricing",
                "verified_at": "2026-11-10",
            }
        ],
    }
    text = json.dumps(catalog, ensure_ascii=False)
    path = tmp_path / "catalog.json"
    path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(
        jcs_canonicalize(json.loads(text)).encode("utf-8")
    ).hexdigest()
    settings = Settings(
        pricing_catalog_path=str(path),
        pricing_catalog_expected_sha256=sha,
    )
    ts.configure_embedding_cost_calculator(
        make_embedding_cost_calculator(settings)
    )


def test_composition_calculator_unknown_after_unique_resolution_keeps_price_id(
    span_exporter, cost_calculator, tmp_path
):
    # 唯一命中价格项后的 unknown（amount_unrepresentable）必须把 price_id
    # 带过组合根并投影到 Span（评审修复：组合根 unknown 分支漏传 price_id）
    _configure_catalog_calculator(tmp_path, "1e400")
    try:
        with ts.embedding_observer("bge-m3") as scope:
            scope.set_input_tokens(1)
    finally:
        ts.configure_embedding_cost_calculator()
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "unknown"
    assert span.attributes["nkb.model.cost_reason"] == "amount_unrepresentable"
    assert span.attributes["nkb.model.price_id"] == "p-tiny"
    assert span.attributes["nkb.model.pricing_version"].startswith("test-cost-fix-v1@")
    assert "nkb.model.cost_usd" not in span.attributes
    assert "nkb.model.cost_decimal" not in span.attributes


def test_composition_calculator_tiny_price_estimated_projects_cost_decimal(
    span_exporter, cost_calculator, tmp_path
):
    # 1e-70 单价 x 1 Token = 1e-76：金额 double 可表示，规范 cost_decimal
    # 字符串 78 字符必须完整投影（评审修复：64 字符契约上限静默丢字段）
    _configure_catalog_calculator(tmp_path, "1e-70")
    try:
        with ts.embedding_observer("bge-m3") as scope:
            scope.set_input_tokens(1)
    finally:
        ts.configure_embedding_cost_calculator()
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "estimated"
    assert span.attributes["nkb.model.cost_decimal"] == "0." + "0" * 75 + "1"
    assert span.attributes["nkb.model.cost_usd"] == float("1e-76")
    assert span.attributes["nkb.model.price_id"] == "p-tiny"


def test_embedding_cost_receives_protocol_facts_with_raw_model(cost_calculator):
    captured = {}
    ts._cost_calculator = lambda facts: captured.update(facts) or {
        "status": "unknown", "reason": "missing_usage", "currency": "USD",
    }
    with ts.embedding_observer("facts-model"):
        pass
    assert captured["operation"] == "embeddings"
    assert captured["raw_model"] == "facts-model"
    assert captured["input_state"] == "unknown"


def test_embedding_cost_invalid_state_not_folded_into_missing(cost_calculator):
    captured = {}
    ts._cost_calculator = lambda facts: captured.update(facts) or {
        "status": "unknown", "reason": "invalid_usage", "currency": "USD",
    }
    with ts.embedding_observer("invalid-model") as scope:
        scope.set_input_tokens("7")
    assert captured["input_state"] == "invalid"
    assert captured["input_value"] == "7"


def test_embedding_cost_malformed_estimated_downgrades_and_counts_attempt(
    reader, span_exporter, cost_calculator
):
    # 畸形 estimated（缺版本）：Metric 仍按 unknown 计 attempts，无任何金额出口
    ts._cost_calculator = lambda facts: _estimated_result(
        pricing_version="", billing_model="downgrade-model"
    )
    with ts.embedding_observer("downgrade-model") as scope:
        scope.set_input_tokens(5)
    span = _last_span(span_exporter)
    assert "nkb.model.cost_status" not in span.attributes
    assert "nkb.model.cost_usd" not in span.attributes
    dims = {**_COST_DIMS, "nkb.model.billing_model": "downgrade-model"}
    assert (
        _counter_delta(
            reader,
            "nkb.model.cost_attempts",
            {**dims, "nkb.model.cost_status": "unknown", "nkb.model.cost_reason": "pricing_unavailable"},
        )
        == 1
    )
    assert _counter_delta(reader, "nkb.model.cost", dims) == 0

    # 金额不可表示（NaN）：降级为 amount_unrepresentable
    ts._cost_calculator = lambda facts: _estimated_result(
        amount=float("nan"), billing_model="nan-model"
    )
    with ts.embedding_observer("nan-model") as scope:
        scope.set_input_tokens(5)
    dims = {**_COST_DIMS, "nkb.model.billing_model": "nan-model"}
    assert (
        _counter_delta(
            reader,
            "nkb.model.cost_attempts",
            {**dims, "nkb.model.cost_status": "unknown", "nkb.model.cost_reason": "amount_unrepresentable"},
        )
        == 1
    )


def test_embedding_without_calculator_records_no_cost_counters(
    reader, span_exporter, cost_calculator
):
    assert ts._cost_calculator is None
    with ts.embedding_observer("nocost-model") as scope:
        scope.set_input_tokens(3)
    span = _last_span(span_exporter)
    for key in (
        "nkb.model.cost_status",
        "nkb.model.cost_reason",
        "nkb.model.cost_usd",
        "nkb.model.pricing_version",
        "nkb.model.billing_provider",
    ):
        assert key not in span.attributes
    for name in ("nkb.model.cost", "nkb.model.cost_components", "nkb.model.cost_attempts"):
        for series in _snapshot(reader).get(name, {}).get("series", {}):
            assert _counter_delta(reader, name, dict(series)) == 0


def test_embedding_observer_maps_label_but_prices_with_trusted_identity(
    span_exporter, cost_calculator
):
    captured = {}
    ts._cost_calculator = lambda facts: captured.update(facts) or _estimated_result(
        billing_model="trusted-model"
    )
    with ts.embedding_observer("bad model!"):
        pass
    span = _last_span(span_exporter)
    # 观测标签走白名单归一；计价身份由受信配置闭包给出，不受请求模型影响
    assert span.attributes["gen_ai.request.model"] == "other"
    assert captured["raw_model"] == "bad model!"
    assert span.attributes["nkb.model.billing_model"] == "trusted-model"


def test_embedding_observer_error_terminal_still_records_cost(
    reader, span_exporter, cost_calculator
):
    # 业务异常路径同样完成计价收口（tokens 已承接的已知事实参与计价）
    ts._cost_calculator = lambda facts: _estimated_result(billing_model="error-model")
    with pytest.raises(ValueError, match="embedding_provider_failed"):
        with ts.embedding_observer("error-cost-model") as scope:
            scope.set_input_tokens(9)
            raise ValueError("embedding_provider_failed")
    dims = {**_COST_DIMS, "nkb.model.billing_model": "error-model"}
    assert _counter_delta(reader, "nkb.model.cost", dims) == 0.00002
    span = _last_span(span_exporter)
    assert span.attributes["nkb.model.cost_status"] == "estimated"
    assert span.attributes["nkb.model.outcome"] == "failure"


def test_map_billing_provider_and_model():
    assert ts.map_billing_provider("ollama") == "ollama"
    assert ts.map_billing_provider("openai") == "other"
    assert ts.map_billing_provider(None) == "other"
    assert ts.map_billing_provider("") == "other"
    assert ts.map_billing_model("bge-m3") == "bge-m3"
    assert ts.map_billing_model("  bge-m3  ") == "bge-m3"
    assert ts.map_billing_model("") == "other"
    assert ts.map_billing_model(7) == "other"
    assert ts.map_billing_model("x" * 65) == "other"
    assert ts.map_billing_model("x" * 64) == "x" * 64
    # 控制字符拒绝与 contract 的 billing_model 校验对齐（Metric 维度
    # 不经过 valid_attribute，必须在此拦下）
    assert ts.map_billing_model("bge\t-m3") == "other"
    assert ts.map_billing_model("bge\n-m3") == "other"
    assert ts.map_billing_model("bge\x7f-m3") == "other"


def test_cost_reason_vocabulary_mirrors_domain_unknown_reasons():
    assert ts._KNOWN_COST_REASONS == frozenset(
        {
            "missing_usage",
            "invalid_usage",
            "price_not_found",
            "price_ambiguous",
            "unsupported_pricing",
            "identity_unresolved",
            "pricing_unavailable",
            "amount_unrepresentable",
        }
    )
