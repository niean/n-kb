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
        (ValueError("boom"), "error"),
        (RuntimeError("embedding_provider_failed"), "error"),
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
    assert ts.retrieval_attrs(7) == {"nkb.result_count": 7}
    assert ts.retrieval_attrs(None) == {}


def test_retrieval_attrs_filter_counts_without_values():
    attrs = ts.retrieval_attrs(
        3,
        _Filter(tags={"a": "secret-value", "b": "other-tag"}, source_kind="upload", min_score=0.42),
    )
    assert attrs == {
        "nkb.result_count": 3,
        "nkb.filter.tags": 2,
        "nkb.filter.source_kind": 1,
        "nkb.filter.min_score": 1,
    }
    dumped = json.dumps(attrs)
    assert "secret-value" not in dumped
    assert "upload" not in dumped
    assert "0.42" not in dumped


def test_retrieval_attrs_accepts_mapping_and_drops_unknown_keys():
    attrs = ts.retrieval_attrs(1, {"tags": {"k": "v"}, "intruder": 3})
    assert attrs == {"nkb.result_count": 1, "nkb.filter.tags": 1}


# ---------------------------------------------------------------------------
# span helpers
# ---------------------------------------------------------------------------


def test_retrieval_span_success_is_internal_and_current(span_exporter):
    with ts.retrieval_span() as span:
        assert trace.get_current_span() is span
        assert span.is_recording()
    assert trace.get_current_span() is not span
    recorded = _last_span(span_exporter)
    assert recorded.name == "nkb.retrieval.search"
    assert recorded.kind == trace.SpanKind.INTERNAL
    assert recorded.attributes.get("nkb.outcome") == "success"
    assert recorded.end_time is not None


def test_retrieval_span_error_marks_safe_error_type_without_events(span_exporter):
    with pytest.raises(ValueError, match="embedding_provider_failed"):
        with ts.retrieval_span():
            raise ValueError("embedding_provider_failed")
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("error.type") == "embedding_failed"
    assert recorded.attributes.get("nkb.outcome") == "error"
    assert recorded.status.status_code == trace.StatusCode.ERROR
    assert recorded.events == ()


def test_retrieval_span_error_does_not_leak_raw_message(span_exporter):
    secret = "raw failure containing https://user:pass@host/db?q=secret"
    with pytest.raises(RuntimeError):
        with ts.retrieval_span():
            raise RuntimeError(secret)
    recorded = _last_span(span_exporter)
    dumped = json.dumps(recorded.attributes, default=str)
    assert secret not in dumped
    assert "user:pass" not in dumped
    assert recorded.events == ()


def test_retrieval_span_cancelled_is_outcome_without_error_type(span_exporter):
    with pytest.raises(asyncio.CancelledError):
        with ts.retrieval_span():
            raise asyncio.CancelledError()
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.outcome") == "cancelled"
    assert "error.type" not in recorded.attributes
    assert recorded.status.status_code != trace.StatusCode.ERROR
    assert recorded.events == ()


def test_index_run_span_records_document_id(span_exporter):
    with ts.index_run_span("doc-123"):
        pass
    recorded = _last_span(span_exporter)
    assert recorded.name == "nkb.index.run"
    assert recorded.attributes.get("nkb.document_id") == "doc-123"
    assert recorded.attributes.get("nkb.outcome") == "success"


def test_index_run_span_without_document_id_omits_attribute(span_exporter):
    with ts.index_run_span():
        pass
    recorded = _last_span(span_exporter)
    assert "nkb.document_id" not in recorded.attributes


@pytest.mark.parametrize(
    ("stage", "expected_name"),
    [
        ("parse", "nkb.index.parse"),
        ("split", "nkb.index.split"),
        ("embed", "nkb.index.embed"),
        ("write", "nkb.index.write"),
        ("unknown-stage", "nkb.index.other"),
    ],
)
def test_index_stage_span_names(span_exporter, stage, expected_name):
    with ts.index_stage_span(stage):
        pass
    recorded = _last_span(span_exporter)
    assert recorded.name == expected_name
    assert recorded.kind == trace.SpanKind.INTERNAL
    assert recorded.attributes.get("nkb.outcome") == "success"


def test_index_stage_span_failure_outcome(span_exporter):
    with pytest.raises(RuntimeError):
        with ts.index_stage_span("write"):
            raise RuntimeError("embedding_provider_failed")
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.outcome") == "error"
    assert recorded.attributes.get("error.type") == "embedding_failed"


def test_set_index_chunk_count_attribute(span_exporter):
    with ts.index_run_span("doc-1") as span:
        ts.set_index_chunk_count(span, 12)
    recorded = _last_span(span_exporter)
    assert recorded.attributes.get("nkb.chunk_count") == 12


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
    ts.record_retrieval("success", result_count=3, seconds=0.5)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"outcome": "success"}) == 1
    results = _hist_delta(reader, "nkb.retrieval.results", {})
    assert results["count"] == 1
    assert results["sum"] == 3
    duration = _hist_delta(reader, "nkb.retrieval.duration", {"outcome": "success"})
    assert duration["count"] == 1
    assert duration["sum"] == pytest.approx(0.5)


def test_record_retrieval_error_records_requests_and_duration_not_results(reader):
    ts.record_retrieval("error", result_count=9, seconds=0.2)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"outcome": "error"}) == 1
    assert _hist_delta(reader, "nkb.retrieval.results", {})["count"] == 0
    duration = _hist_delta(reader, "nkb.retrieval.duration", {"outcome": "error"})
    assert duration["count"] == 1
    assert duration["sum"] == pytest.approx(0.2)


def test_record_retrieval_cancelled_outcome(reader):
    ts.record_retrieval("cancelled", seconds=0.1)
    assert _counter_delta(reader, "nkb.retrieval.requests", {"outcome": "cancelled"}) == 1
    assert _hist_delta(reader, "nkb.retrieval.results", {})["count"] == 0
    assert _hist_delta(reader, "nkb.retrieval.duration", {"outcome": "cancelled"})["count"] == 1


def test_record_index_success_records_jobs_and_chunks(reader):
    ts.record_index("success", chunk_count=12)
    assert _counter_delta(reader, "nkb.index.jobs", {"outcome": "success"}) == 1
    chunks = _hist_delta(reader, "nkb.index.chunks", {})
    assert chunks["count"] == 1
    assert chunks["sum"] == 12


def test_record_index_error_and_cancelled_skip_chunks(reader):
    ts.record_index("error", chunk_count=5)
    ts.record_index("cancelled")
    assert _counter_delta(reader, "nkb.index.jobs", {"outcome": "error"}) == 1
    assert _counter_delta(reader, "nkb.index.jobs", {"outcome": "cancelled"}) == 1
    assert _hist_delta(reader, "nkb.index.chunks", {})["count"] == 0


def test_record_helpers_isolated_from_meter_failure(monkeypatch):
    def broken_meter(*args, **kwargs):
        raise RuntimeError("meter exploded")

    monkeypatch.setattr(ts.metrics, "get_meter", broken_meter)
    ts.record_retrieval("success", result_count=1, seconds=0.1)
    ts.record_index("success", chunk_count=1)


# ---------------------------------------------------------------------------
# 七个 instrument 的名称与单位
# ---------------------------------------------------------------------------


def test_business_instruments_exact_names_and_units(reader):
    with ts.embedding_observer("unit-check-model") as scope:
        scope.set_input_tokens(4)
    ts.record_retrieval("success", result_count=1, seconds=0.01)
    ts.record_index("success", chunk_count=1)

    with ts.operation_span("nkb.document.upload"):
        pass
    with ts.operation_span("nkb.mcp.status"):
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
    assert recorded.attributes.get("nkb.outcome") == "success"
    assert recorded.events == ()


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
    assert recorded.attributes.get("nkb.outcome") == "error"
    assert recorded.events == ()


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
    assert recorded.attributes.get("nkb.outcome") == "cancelled"
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
    with ts.operation_context(operation="outer"):
        assert ts.current_operation_context()["operation"] == "outer"
        with ts.operation_context(stage="embed"):
            current = ts.current_operation_context()
            assert current["operation"] == "outer"
            assert current["stage"] == "embed"
        assert ts.current_operation_context() == {"operation": "outer"}
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
        with ts.operation_context(operation=name):
            await asyncio.sleep(0.01)
            results[name] = ts.current_operation_context()["operation"]

    async def main() -> None:
        await asyncio.gather(worker("task-a"), worker("task-b"))

    asyncio.run(main())
    assert results == {"task-a": "task-a", "task-b": "task-b"}


def test_span_helpers_update_operation_context(span_exporter):
    with ts.retrieval_span():
        assert ts.current_operation_context()["operation"] == "nkb.retrieval.search"
        with ts.index_stage_span("embed"):
            assert ts.current_operation_context()["stage"] == "embed"
        assert "stage" not in ts.current_operation_context()
    assert ts.current_operation_context() == {}
    with ts.embedding_observer("ctx-model"):
        assert ts.current_operation_context()["operation"] == "embeddings"


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


@pytest.mark.parametrize('outcome', ['success', 'error', 'cancelled'])
def test_completed_operation_records_once_and_preserves_exception(reader, outcome):
    before = _snapshot(reader)
    error = asyncio.CancelledError() if outcome == 'cancelled' else ValueError('private')
    try:
        with ts.completed_operation('nkb.document.upload'):
            if outcome != 'success':
                raise error
    except BaseException as caught:
        assert caught is error
    state = _snapshot(reader)
    key = frozenset({'outcome':outcome}.items())
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
