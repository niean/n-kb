"""T4: app/infrastructure/observability/setup.py 行为测试。

覆盖计划矩阵：四信号成功/失败/恢复注入、unknown、无数据、队列满丢弃计数、
并发快照、配置冲突、部分初始化失败、共享关闭期限、引用计数释放。
除标注外全部使用 FakeLoader 隔离全局 Provider；真实 SDK 路径用
InMemory 导出器/子进程冒烟验证，不污染 pytest 进程全局状态。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.trace.status import Status, StatusCode

from app.config import Settings
from app.infrastructure.observability import setup


# ---------------------------------------------------------------------------
# Fakes（SDK loader 注入点；全局 setter/getter 全部被记录、不触真实全局态）
# ---------------------------------------------------------------------------


class FakeResource:
    def __init__(self, attributes=None):
        self.attributes = dict(attributes or {})


class FakeExporter:
    """Fake OTLP exporter：记录装配 kwargs 与导出结果。"""

    instances: list["FakeExporter"] = []

    def __init__(self, endpoint=None, insecure=None, timeout=None, **kwargs):
        self.kwargs = {"endpoint": endpoint, "insecure": insecure, "timeout": timeout}
        self.extra_kwargs = kwargs
        self.exports: list[object] = []
        self.fail_exports = False
        self.shutdown_calls = 0
        self.force_flush_calls = 0
        type(self).instances.append(self)

    def export(self, batch, timeout_millis=None, **kwargs):
        self.exports.append(batch)
        if self.fail_exports:
            return (
                MetricExportResult.FAILURE
                if isinstance(self, FakeMetricExporter)
                else SpanExportResult.FAILURE
            )
        return (
            MetricExportResult.SUCCESS
            if isinstance(self, FakeMetricExporter)
            else SpanExportResult.SUCCESS
        )

    def force_flush(self, timeout_millis=None):
        self.force_flush_calls += 1
        return True

    def shutdown(self, timeout_millis=None, **kwargs):
        self.shutdown_calls += 1
        return True


class FakeSpanExporter(FakeExporter):
    pass


class FakeMetricExporter(FakeExporter):
    pass


class FakeLogExporter(FakeExporter):
    pass


class FailingMetricExporter(FakeMetricExporter):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("boom: metric exporter construction failed")


class FakeBatchProcessor:
    def __init__(
        self,
        exporter,
        max_queue_size=None,
        max_export_batch_size=None,
        export_timeout_millis=None,
        **kwargs,
    ):
        self.exporter = exporter
        self.kwargs = {
            "max_queue_size": max_queue_size,
            "max_export_batch_size": max_export_batch_size,
            "export_timeout_millis": export_timeout_millis,
        }

    def shutdown(self, timeout_millis=None):
        return True

    def force_flush(self, timeout_millis=None):
        return True


class FakeMetricReader:
    def __init__(self, exporter, export_interval_millis=None,
                 export_timeout_millis=None, **kwargs):
        self.exporter = exporter
        self.kwargs = {
            "export_interval_millis": export_interval_millis,
            "export_timeout_millis": export_timeout_millis,
        }

    def shutdown(self, timeout_millis=None, **kwargs):
        return True

    def force_flush(self, timeout_millis=None):
        return True


class FakeProvider:
    """Fake Tracer/Meter/Logger provider with event recording."""

    instances: list["FakeProvider"] = []

    def __init__(self, resource=None, sampler=None, metric_readers=None, **kwargs):
        type(self).instances.append(self)
        self.resource = resource
        self.sampler = sampler
        self.metric_readers = list(metric_readers or [])
        self.kwargs = dict(
            resource=resource, sampler=sampler, metric_readers=self.metric_readers,
            **kwargs,
        )
        self.processors: list[object] = []
        self.events: list[tuple] = []
        self.event_lock = threading.Lock()
        self.block_seconds = 0.0

    def _record(self, event):
        with self.event_lock:
            self.events.append(event)

    def add_span_processor(self, processor):
        self.processors.append(processor)

    add_log_record_processor = add_span_processor

    def force_flush(self, timeout_millis=None):
        self._record(("force_flush", timeout_millis))
        if self.block_seconds:
            time.sleep(self.block_seconds)
        return True

    def shutdown(self):
        self._record(("shutdown",))
        if self.block_seconds:
            time.sleep(self.block_seconds)
        return True


class _ProxyTracerProvider:
    pass


class _ProxyMeterProvider:
    pass


class _ProxyLoggerProvider:
    pass


_PROXY_PROVIDER_NAMES = {
    "ProxyTracerProvider",
    "_ProxyMeterProvider",
    "ProxyLoggerProvider",
}


class FakeLoader:
    def __init__(self, *, claimed_tracer=False, claimed_meter=False,
                 claimed_logger=False, metric_exporter_cls=FakeMetricExporter):
        self.Resource = FakeResource
        self.OTLPSpanExporter = FakeSpanExporter
        self.OTLPMetricExporter = metric_exporter_cls
        self.OTLPLogExporter = FakeLogExporter
        self.BatchSpanProcessor = FakeBatchProcessor
        self.BatchLogRecordProcessor = FakeBatchProcessor
        self.PeriodicExportingMetricReader = FakeMetricReader
        self.TracerProvider = FakeProvider
        self.MeterProvider = FakeProvider
        self.LoggerProvider = FakeProvider
        self.set_tracer_calls: list[object] = []
        self.set_meter_calls: list[object] = []
        self.set_logger_calls: list[object] = []
        self._claimed_tracer = claimed_tracer
        self._claimed_meter = claimed_meter
        self._claimed_logger = claimed_logger
        self._tracer_provider = (
            SimpleNamespace(external="tracer") if claimed_tracer
            else _ProxyTracerProvider()
        )
        self._meter_provider = (
            SimpleNamespace(external="meter") if claimed_meter
            else _ProxyMeterProvider()
        )
        self._logger_provider = (
            SimpleNamespace(external="logger") if claimed_logger
            else _ProxyLoggerProvider()
        )

    def set_tracer_provider(self, provider):
        self.set_tracer_calls.append(provider)

    def set_meter_provider(self, provider):
        self.set_meter_calls.append(provider)

    def set_logger_provider(self, provider):
        self.set_logger_calls.append(provider)

    def get_tracer_provider(self):
        return None if not self._claimed_tracer else self._tracer_provider

    def get_meter_provider(self):
        return None if not self._claimed_meter else self._meter_provider

    def get_logger_provider(self):
        return None if not self._claimed_logger else self._logger_provider

    def is_unclaimed(self, provider):
        return provider is None or type(provider).__name__ in _PROXY_PROVIDER_NAMES


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _reset_setup_state():
    setup._reset_for_tests()
    FakeExporter.instances = []
    FakeProvider.instances = []
    yield
    setup._reset_for_tests()
    FakeExporter.instances = []
    FakeProvider.instances = []


_ENDPOINT = "http://otel-collector:4317"


def make_handle(loader=None, **overrides):
    kwargs = {"otel_enabled": True, "otel_exporter_otlp_endpoint": _ENDPOINT}
    kwargs.update(overrides)
    return setup.setup_observability(_settings(**kwargs), sdk_loader=loader)


def make_enabled_loader() -> FakeLoader:
    FakeExporter.instances = []
    return FakeLoader()


def ring_of(handle) -> setup._RingBufferSpanProcessor:
    return handle._ring_buffer


def make_ended_span(
    name="op",
    sampled=True,
    trace_id=0x11111111111111111111111111111111,
    span_id=0x2222222222222222,
    parent_span_id=None,
    attributes=None,
):
    ctx = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
    )
    parent = None
    if parent_span_id is not None:
        parent = SpanContext(
            trace_id=trace_id,
            span_id=parent_span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
    return SimpleNamespace(
        name=name,
        context=ctx,
        parent=parent,
        start_time=1_000_000_000,
        end_time=1_500_000_000,
        status=Status(StatusCode.OK),
        attributes=dict(attributes or {}),
    )


# ---------------------------------------------------------------------------
# 禁用状态：返回 None，零 SDK import，不装配任何东西
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_returns_none_without_any_assembly(self):
        class ExplodingLoader:
            def __getattr__(self, name):
                raise AssertionError(
                    f"disabled setup must not touch the SDK loader (got {name})"
                )

        assert setup.setup_observability(
            _settings(otel_enabled=False), sdk_loader=ExplodingLoader()
        ) is None
        # 也不残留可复用的运行时
        assert setup.get_runtime_handle() is None

    def test_disabled_does_not_import_sdk_modules(self, monkeypatch):
        for mod in list(sys.modules):
            if mod.startswith("opentelemetry.sdk") or mod.startswith(
                "opentelemetry.exporter"
            ):
                monkeypatch.delitem(sys.modules, mod)
        setup._reset_for_tests()
        setup.setup_observability(_settings(otel_enabled=False))
        assert not any(
            m.startswith("opentelemetry.sdk") or m.startswith("opentelemetry.exporter")
            for m in sys.modules
        )

    def test_disabled_does_not_wire_details_or_drop_handler(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            setup, "configure_details", lambda *a, **k: calls.append(a)
        )
        setup.setup_observability(_settings(otel_enabled=False))
        assert calls == []
        assert setup._drop_handler_attached() is False


# ---------------------------------------------------------------------------
# 装配（enabled + endpoint）
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_otlp_assembly_params_http(self):
        loader = make_enabled_loader()
        handle = make_handle(loader, otel_traces_sample_ratio=0.5)
        assert handle.status == "healthy"
        snap = handle.snapshot()
        assert snap["mode"] == "export"
        assert snap["sample_ratio"] == 0.5
        assert snap["instance_id"] == handle.instance_id
        assert snap["status"] == "healthy"

        for exporter in FakeExporter.instances:
            assert exporter.kwargs["endpoint"] == _ENDPOINT
            assert exporter.kwargs["insecure"] is True
            assert exporter.kwargs["timeout"] == 3.0

        tracer_provider = loader.set_tracer_calls[0]
        attrs = tracer_provider.resource.attributes
        assert attrs["service.name"] == "n-kb"
        assert attrs["service.version"]
        assert attrs["service.instance.id"] == snap["instance_id"]

        sampler = tracer_provider.sampler
        assert type(sampler).__name__ == "ParentBased"
        assert sampler._root.rate == 0.5

        bsp = [
            p for p in tracer_provider.processors if isinstance(p, FakeBatchProcessor)
        ]
        assert len(bsp) == 1
        assert bsp[0].kwargs["max_queue_size"] == 2048
        assert bsp[0].kwargs["max_export_batch_size"] == 512
        assert bsp[0].kwargs["export_timeout_millis"] == 3000.0
        # ring buffer 挂在 tracer provider 上
        assert ring_of(handle) in tracer_provider.processors

        meter_provider = loader.set_meter_calls[0]
        reader = meter_provider.metric_readers[0]
        assert reader.kwargs["export_interval_millis"] == 15000.0
        assert reader.kwargs["export_timeout_millis"] == 3000.0
        # metric 出口经过 1000 序列有界包装，再经过 delivery 包装
        assert isinstance(reader.exporter._inner, setup._SnapshotMetricExporter)
        bounded = reader.exporter
        assert isinstance(bounded, setup._BoundedCardinalityMetricExporter)
        assert isinstance(bounded._inner._inner, setup._DeliveryStatusWrapper)

        logger_provider = loader.set_logger_calls[0]
        log_processors = [
            p for p in logger_provider.processors if isinstance(p, FakeBatchProcessor)
        ]
        assert len(log_processors) == 1
        assert log_processors[0].kwargs["max_queue_size"] == 2048
        assert log_processors[0].kwargs["max_export_batch_size"] == 512

    def test_https_endpoint_is_secure(self):
        loader = make_enabled_loader()
        make_handle(loader, otel_exporter_otlp_endpoint="https://collector.example.com:4317")
        for exporter in FakeExporter.instances:
            assert exporter.kwargs["insecure"] is False

    def test_initial_delivery_unknown_and_no_data(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        snap = handle.snapshot()
        for name in ("traces", "metrics", "logs"):
            signal = snap["signals"][name]
            assert signal["status"] == "healthy"
            assert signal["delivery"] == "unknown"
            assert signal["attempt_count"] == 0
            assert signal["success_count"] == 0
            assert signal["failure_count"] == 0
            assert signal["last_success"] is None
            assert signal["last_failure"] is None
            assert signal["current_error"] is None
        # 无数据时 spans 为空
        assert snap["spans"] == []

    def test_profile_disabled_by_default(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        snap = handle.snapshot()
        profile = snap["signals"]["profile"]
        assert profile["status"] == "disabled"
        assert profile["delivery"] == "disabled"
        assert snap["status"] == "healthy"

    def test_profile_enabled_without_worker_degraded(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader,
            profiling_enabled=True,
            profiling_pyroscope_endpoint="http://pyroscope:4040",
        )
        snap = handle.snapshot()
        assert snap["signals"]["profile"]["status"] == "degraded"
        assert snap["signals"]["profile"]["current_error"] == "profiler_not_configured"
        assert snap["status"] == "degraded"

    def test_meter_provider_exemplar_filter_trace_based(self):
        # P062：exemplar 默认开启（trace-based），不得传 AlwaysOff。
        loader = make_enabled_loader()
        make_handle(loader)
        meter_provider = loader.set_meter_calls[0]
        assert type(meter_provider.kwargs["exemplar_filter"]).__name__ == (
            "TraceBasedExemplarFilter"
        )

    def test_details_wired_on_setup(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            setup, "configure_details", lambda sanitizer, max_bytes, **k: calls.append((sanitizer, max_bytes))
        )
        make_handle(loader=make_enabled_loader(), otel_content_max_bytes=1024)
        assert len(calls) == 1
        sanitizer_fn, max_bytes = calls[0]
        assert callable(sanitizer_fn)
        assert max_bytes == 1024
        # 注入的 sanitizer 函数真实脱敏
        assert sanitizer_fn("Bearer abc123") != "Bearer abc123"

    def test_drop_handler_attached_and_removed(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        assert setup._drop_handler_attached() is True
        handle.release()
        assert setup._drop_handler_attached() is False

    def test_snapshot_pure_json_and_semantics_injection(self):
        stub = SimpleNamespace(
            SEMCONV_VERSION="stub-semconv-1",
            INSTRUMENTS={
                "nkb.retrieval.requests": SimpleNamespace(
                    name="nkb.retrieval.requests", kind="counter", unit="1",
                    allowed_dimensions=("outcome",),
                )
            },
        )
        loader = make_enabled_loader()
        handle = setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT),
            sdk_loader=loader,
            semantics=stub,
            semantics_keys={"outcome": "nkb.outcome", "error_type": "error.type"},
        )
        snap = handle.snapshot()
        assert json.loads(json.dumps(snap)) == snap
        assert snap["semconv_version"] == "stub-semconv-1"
        assert [i["name"] for i in snap["instruments"]] == ["nkb.retrieval.requests"]


# ---------------------------------------------------------------------------
# 引用计数与共享
# ---------------------------------------------------------------------------


class TestReferenceCounting:
    def test_same_config_shares_runtime(self):
        loader = make_enabled_loader()
        h1 = make_handle(loader)
        h2 = setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT),
            sdk_loader=loader,
        )
        assert h2 is h1
        assert len(loader.set_tracer_calls) == 1
        assert len(FakeProvider.instances) == 3  # tracer + meter + logger

    def test_release_refcount_then_shutdown_on_last(self, monkeypatch):
        details_calls = []
        monkeypatch.setattr(
            setup, "configure_details", lambda *a, **k: details_calls.append(a)
        )
        loader = make_enabled_loader()
        handle = make_handle(loader)
        # 第二个使用者共享同一运行时
        second = setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT),
            sdk_loader=loader,
        )
        assert second is handle
        assert handle.release() == 1  # 仍有一个使用者
        assert loader.set_tracer_calls[0].events == []  # 未 shutdown
        assert setup.get_runtime_handle() is handle
        assert handle.release() == 0  # 最后引用 -> shutdown
        assert setup.get_runtime_handle() is None
        assert ("shutdown",) in [ev for ev in loader.set_tracer_calls[0].events]
        assert ("shutdown",) in [ev for ev in loader.set_meter_calls[0].events]
        assert ("shutdown",) in [ev for ev in loader.set_logger_calls[0].events]
        # 最后释放才解除 details 配置
        assert details_calls and details_calls[-1][0] is None

    def test_release_repeat_safe(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        handle.release()
        handle.release()
        handle.release()

    def test_setup_after_shutdown_returns_degraded_not_resurrected(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        handle.release()
        provider_count = len(FakeProvider.instances)
        second = setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT),
            sdk_loader=loader,
        )
        # 全局 Provider 已发布过且已关闭，不可复活：返回独立 degraded 诊断句柄
        assert second is not handle
        assert second.status == "degraded"
        snap = second.snapshot()
        assert snap["diagnostic_error"] == "runtime_already_shutdown"
        # 不重新装配任何 Provider
        assert len(FakeProvider.instances) == provider_count
        assert len(loader.set_tracer_calls) == 1


# ---------------------------------------------------------------------------
# 配置冲突
# ---------------------------------------------------------------------------


class TestConfigConflict:
    def test_conflicting_ratio_returns_independent_degraded_handle(self):
        loader = make_enabled_loader()
        first = make_handle(loader, otel_traces_sample_ratio=1.0)
        provider_count = len(FakeProvider.instances)
        second = setup.setup_observability(
            _settings(
                otel_enabled=True,
                otel_exporter_otlp_endpoint=_ENDPOINT,
                otel_traces_sample_ratio=0.5,
            ),
            sdk_loader=loader,
        )
        assert second is not first
        assert second.status == "degraded"
        snap = second.snapshot()
        assert snap["diagnostic_error"] == "config_conflict"
        assert "otel_traces_sample_ratio" in snap["differing_fields"]
        assert snap["sample_ratio"] == 0.5  # 报告自己的配置，不改写他人
        # 原使用者不受影响
        assert first.status == "healthy"
        assert first.snapshot()["sample_ratio"] == 1.0
        # 不装配新组件
        assert len(FakeProvider.instances) == provider_count
        assert len(loader.set_tracer_calls) == 1

    def test_content_limit_and_diag_links_participate_in_conflict(self):
        loader = make_enabled_loader()
        make_handle(loader, otel_content_max_bytes=2048)
        for field, value in (
            ("otel_content_max_bytes", 4096),
            ("otel_grafana_base_url", "http://grafana:3000"),
            ("otel_grafana_traces_base_url", "http://grafana:3000/trace"),
        ):
            conflict = setup.setup_observability(
                _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT,
                          **{field: value}),
                sdk_loader=loader,
            )
            assert conflict.status == "degraded", field
            assert conflict.snapshot()["diagnostic_error"] == "config_conflict", field

    def test_conflict_release_does_not_touch_shared_runtime(self):
        loader = make_enabled_loader()
        first = make_handle(loader)
        conflict = setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT,
                      otel_traces_sample_ratio=0.1),
            sdk_loader=loader,
        )
        conflict.release()  # 诊断句柄：不 acquire、不影响引用计数
        assert first.snapshot()["status"] == "healthy"
        assert setup.get_runtime_handle() is first
        assert [ev[0] for ev in loader.set_tracer_calls[0].events] == []
        # 原句柄仍然正常释放
        assert first.release() == 0
        assert ("shutdown",) in [ev for ev in loader.set_tracer_calls[0].events]


# ---------------------------------------------------------------------------
# 部分初始化失败
# ---------------------------------------------------------------------------


class TestPartialFailure:
    def test_partial_failure_rolls_back_and_allows_retry(self):
        loader = FakeLoader(metric_exporter_cls=FailingMetricExporter)
        handle = make_handle(loader)
        assert handle.status == "degraded"
        snap = handle.snapshot()
        assert snap["status"] == "degraded"
        # 未发布任何全局 Provider
        assert loader.set_tracer_calls == []
        assert loader.set_meter_calls == []
        assert loader.set_logger_calls == []
        # 已构造的 tracer provider 被回滚清理
        built = list(FakeProvider.instances)
        assert built
        assert any("shutdown" in [ev[0] for ev in p.events] for p in built)

    def test_partial_failure_does_not_wire_details(self, monkeypatch):
        calls = []
        monkeypatch.setattr(setup, "configure_details", lambda *a, **k: calls.append(a))
        loader = FakeLoader(metric_exporter_cls=FailingMetricExporter)
        make_handle(loader)
        assert calls == []

    def test_partial_failure_allows_retry_with_good_loader(self):
        bad = FakeLoader(metric_exporter_cls=FailingMetricExporter)
        make_handle(bad, otel_exporter_otlp_endpoint=_ENDPOINT)
        FakeExporter.instances = []
        good = make_enabled_loader()
        handle = make_handle(good, otel_exporter_otlp_endpoint=_ENDPOINT)
        assert handle.status == "healthy"
        assert len(good.set_tracer_calls) == 1

    def test_external_provider_not_replaced(self):
        loader = FakeLoader(claimed_tracer=True)
        handle = make_handle(loader)
        assert loader.set_tracer_calls == []
        assert loader.set_meter_calls  # 其它信号仍装配
        snap = handle.snapshot()
        assert snap["status"] == "degraded"
        assert snap["signals"]["traces"]["status"] == "degraded"
        assert snap["signals"]["metrics"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# local-only
# ---------------------------------------------------------------------------


class TestLocalOnly:
    def test_no_network_exporter_bounded_local_metric(self):
        loader = make_enabled_loader()
        handle = make_handle(loader, otel_exporter_otlp_endpoint="")
        assert handle.status == "healthy"
        snap = handle.snapshot()
        assert snap["mode"] == "local"
        assert FakeExporter.instances == []
        assert len(loader.set_tracer_calls) == 1
        meter_provider = loader.set_meter_calls[0]
        reader = meter_provider.metric_readers[0]
        assert isinstance(reader, FakeMetricReader)
        assert not isinstance(reader.exporter, FakeExporter)
        assert isinstance(reader.exporter, setup._BoundedCardinalityMetricExporter)
        assert hasattr(reader.exporter._inner, "latest")
        for name in ("traces", "metrics"):
            assert snap["signals"][name]["status"] == "healthy"
            assert snap["signals"][name]["delivery"] == "disabled"
        assert snap["signals"]["logs"]["status"] == "disabled"
        assert snap["signals"]["logs"]["delivery"] == "disabled"
        assert loader.set_logger_calls == []

    def test_profile_with_own_endpoint_still_export_capable(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader,
            otel_exporter_otlp_endpoint="",
            profiling_enabled=True,
            profiling_pyroscope_endpoint="http://pyroscope:4040",
        )
        # OTLP 全部 disabled，但 Profile 自带网络上传出口：mode=export
        # （spec：任一 OTLP 或 Profile 网络出口启用为 export）；mode 不
        # 替代 status。
        assert handle.snapshot()["mode"] == "export"
        assert handle.snapshot()["signals"]["profile"]["status"] == "degraded"
        handle.mark_profiler_attached()
        assert handle.snapshot()["signals"]["profile"]["status"] == "healthy"
        # 注：profiling_enabled=true 必须携带非空 pyroscope endpoint（T1
        # 配置校验），"Profile 启用但无网络出口" 在 Settings 层不可表达，
        # 故无需 local 对照用例。


# ---------------------------------------------------------------------------
# Ring buffer：最近 100 条已结束 sampled 安全摘要
# ---------------------------------------------------------------------------


class TestRingBuffer:
    _SAFE_KEYS = {
        "name", "trace_id", "span_id", "parent_span_id", "status",
        "outcome", "error_type", "duration_ms",
    }

    def _handle_with_keys(self):
        return setup.setup_observability(
            _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT),
            sdk_loader=make_enabled_loader(),
            semantics_keys={"outcome": "nkb.outcome", "error_type": "error.type"},
        )

    def test_only_ended_sampled_spans(self):
        ring = setup._RingBufferSpanProcessor()
        ring.on_start(SimpleNamespace(), None)
        assert ring.snapshot_spans() == []
        ring.on_end(make_ended_span(name="ok"))
        ring.on_end(make_ended_span(name="unsampled", sampled=False))
        assert [s["name"] for s in ring.snapshot_spans()] == ["ok"]

    def test_safe_projection_only(self):
        handle = self._handle_with_keys()
        ring = ring_of(handle)
        attrs = {
            "nkb.outcome": "success",
            "error.type": "embedding_failed",
            "http.url": "http://secret.example.com/?q=leak",
            "sensitive.free.text": "y" * 500,
            "nkb.document_id": "doc-123",
        }
        ring.on_end(make_ended_span(name="x" * 500, attributes=attrs))
        entry = handle.snapshot()["spans"][0]
        assert set(entry.keys()) == self._SAFE_KEYS
        assert entry["name"] == "x" * 256  # 限长
        assert entry["outcome"] == "success"
        assert entry["error_type"] == "embedding_failed"
        assert entry["trace_id"] == "11111111111111111111111111111111"
        assert entry["span_id"] == "2222222222222222"
        assert entry["status"] == "OK"
        assert entry["duration_ms"] == 500.0
        # 任何属性原文都不进入快照
        assert "http.url" not in json.dumps(entry)
        assert "doc-123" not in json.dumps(entry)

    def test_without_injected_keys_no_attribute_projection(self):
        handle = make_handle(make_enabled_loader())
        ring = ring_of(handle)
        ring.on_end(make_ended_span(attributes={"nkb.outcome": "success"}))
        entry = handle.snapshot()["spans"][0]
        assert entry["outcome"] is None
        assert entry["error_type"] is None

    def test_cap_100_oldest_evicted(self):
        handle = self._handle_with_keys()
        ring = ring_of(handle)
        for i in range(120):
            ring.on_end(make_ended_span(name=f"op-{i}", span_id=i + 1))
        spans = handle.snapshot()["spans"]
        assert len(spans) == 100
        assert spans[0]["name"] == "op-20"
        assert spans[-1]["name"] == "op-119"

    def test_concurrent_on_end_and_snapshot(self):
        handle = self._handle_with_keys()
        ring = ring_of(handle)
        errors: list[BaseException] = []

        def producer(base: int) -> None:
            try:
                for i in range(200):
                    ring.on_end(
                        make_ended_span(name=f"t{base}-span-{i}",
                                        span_id=base * 1000 + i)
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(200):
                    snap = handle.snapshot()
                    assert isinstance(snap["spans"], list)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=producer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(ring.snapshot_spans()) == 100

    def test_snapshot_returns_copies_not_live_records(self):
        handle = self._handle_with_keys()
        ring = ring_of(handle)
        ring.on_end(make_ended_span(name="a"))
        spans = handle.snapshot()["spans"]
        spans.append({"name": "injected"})
        spans[0]["name"] = "mutated"
        again = handle.snapshot()["spans"]
        assert len(again) == 1
        assert again[0]["name"] == "a"


# ---------------------------------------------------------------------------
# 四信号成功/失败/恢复注入
# ---------------------------------------------------------------------------


class TestDeliveryStatus:
    def _wrapper_for_traces(self, loader):
        handle = make_handle(loader)
        tracer_provider = loader.set_tracer_calls[0]
        bsp = next(
            p for p in tracer_provider.processors
            if isinstance(p, FakeBatchProcessor)
        )
        return handle, bsp.exporter

    def test_success_failure_recovery_per_signal(self):
        loader = make_enabled_loader()
        handle, wrapper = self._wrapper_for_traces(loader)

        wrapper.export([object()])
        snap = handle.snapshot()["signals"]["traces"]
        assert snap["delivery"] == "succeeded"
        assert snap["attempt_count"] == 1
        assert snap["success_count"] == 1
        assert snap["failure_count"] == 0
        assert snap["last_success"]

        wrapper.exporter.fail_exports = True
        wrapper.export([object()])
        snap = handle.snapshot()["signals"]["traces"]
        assert snap["delivery"] == "failed"
        assert snap["attempt_count"] == 2
        assert snap["success_count"] == 1
        assert snap["failure_count"] == 1
        assert snap["last_failure"]
        assert snap["current_error"]
        assert handle.snapshot()["status"] == "degraded"

        wrapper.exporter.fail_exports = False
        wrapper.export([object()])
        snap = handle.snapshot()["signals"]["traces"]
        # 成功恢复 current 状态，累计失败次数不重置
        assert snap["delivery"] == "succeeded"
        assert snap["status"] == "healthy"
        assert snap["failure_count"] == 1
        assert snap["current_error"] is None
        assert handle.snapshot()["status"] == "healthy"

    def test_wrapper_exporter_exception_is_failure(self):
        loader = make_enabled_loader()
        handle, wrapper = self._wrapper_for_traces(loader)

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        wrapper.exporter.export = boom
        wrapper.export([object()])
        snap = handle.snapshot()["signals"]["traces"]
        assert snap["delivery"] == "failed"
        assert snap["failure_count"] == 1

    def test_force_flush_and_shutdown_never_propagate(self):
        loader = make_enabled_loader()
        handle, wrapper = self._wrapper_for_traces(loader)

        def boom(*args, **kwargs):
            raise RuntimeError("flush/shutdown blew up")

        wrapper.exporter.force_flush = boom
        wrapper.exporter.shutdown = boom
        assert wrapper.force_flush(timeout_millis=100) is False
        wrapper.shutdown()
        snap = handle.snapshot()["signals"]["traces"]
        assert snap["delivery"] == "failed"
        assert snap["failure_count"] == 2
        assert snap["current_error"] == "export_shutdown_failed"

    def test_disabled_signal_not_affected_by_export_events(self):
        loader = make_enabled_loader()
        handle = make_handle(
            loader, profiling_enabled=True,
            profiling_pyroscope_endpoint="http://pyroscope:4040",
        )
        # 未装配 Profile worker：degraded；profiler_on_status 桥接成功/失败
        handle.profiler_on_status()("degraded", "upload_failed")
        assert handle.snapshot()["signals"]["profile"]["failure_count"] == 1
        handle.profiler_on_status()("healthy", None)
        snap = handle.snapshot()["signals"]["profile"]
        assert snap["status"] == "healthy"
        assert snap["delivery"] == "succeeded"
        assert snap["failure_count"] == 1  # 累计不重置


# ---------------------------------------------------------------------------
# 并发快照（delivery 更新与 snapshot 并发）
# ---------------------------------------------------------------------------


class TestConcurrentSnapshot:
    def test_snapshot_under_concurrent_delivery_updates(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        tracer_provider = loader.set_tracer_calls[0]
        bsp = next(
            p for p in tracer_provider.processors
            if isinstance(p, FakeBatchProcessor)
        )
        wrapper = bsp.exporter
        errors: list[BaseException] = []
        stop = threading.Event()

        def exporter_thread() -> None:
            try:
                while not stop.is_set():
                    wrapper.exporter.fail_exports = not wrapper.exporter.fail_exports
                    wrapper.export([object()])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def snapshot_thread() -> None:
            try:
                while not stop.is_set():
                    snap = handle.snapshot()
                    assert snap["status"] in ("healthy", "degraded")
                    assert json.loads(json.dumps(snap)) == snap
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=exporter_thread)]
        threads += [threading.Thread(target=snapshot_thread) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        stop.set()
        for thread in threads:
            thread.join()
        assert errors == []


# ---------------------------------------------------------------------------
# Metric cardinality：每 instrument 含 overflow 最多 1000 条（真实 SDK 数据）
# ---------------------------------------------------------------------------


class CaptureMetricExporter:
    def __init__(self):
        self.batches: list[object] = []

    def export(self, metrics_data, timeout_millis=None, **kwargs):
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=None, **kwargs):
        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis=None, **kwargs):
        return MetricExportResult.SUCCESS


class TestMetricCardinality:
    def _collect(self, instrument_factory, n_series):
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        instrument = instrument_factory(provider.get_meter("card-test"))
        for i in range(n_series):
            if hasattr(instrument, "add"):
                instrument.add(1, {"k": str(i)})
            else:
                instrument.record(i + 1, {"k": str(i)})
        return reader.get_metrics_data()

    def test_sdk_has_no_native_cardinality_limit(self):
        # P057 核验：1.45.1 SDK MeterProvider 无 cardinality 参数 —— 必须由
        # 本模块在导出侧真实兜底，因此先证明 SDK 自身不设限。
        data = self._collect(lambda m: m.create_counter("c"), 1200)
        points = data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        assert len(points) == 1200

    def test_counter_capped_at_1000_with_overflow(self):
        data = self._collect(lambda m: m.create_counter("c"), 1200)
        capture = CaptureMetricExporter()
        bounded = setup._BoundedCardinalityMetricExporter(capture)
        result = bounded.export(data)
        assert result == MetricExportResult.SUCCESS
        points = capture.batches[0].resource_metrics[0].scope_metrics[0] \
            .metrics[0].data.data_points
        assert len(points) == 1000
        overflow = [p for p in points if p.attributes.get("otel.metrics.overflow")]
        assert len(overflow) == 1
        # 溢出点承载被丢弃序列的总量，总和保持不变
        assert sum(p.value for p in points) == 1200
        # 未被截断的 999 条为原始数据
        assert sum(1 for p in points if not p.attributes.get("otel.metrics.overflow")) == 999

    def test_histogram_capped_at_1000_with_overflow(self):
        data = self._collect(lambda m: m.create_histogram("h"), 1200)
        capture = CaptureMetricExporter()
        bounded = setup._BoundedCardinalityMetricExporter(capture)
        bounded.export(data)
        points = capture.batches[0].resource_metrics[0].scope_metrics[0] \
            .metrics[0].data.data_points
        assert len(points) == 1000
        overflow = [p for p in points if p.attributes.get("otel.metrics.overflow")]
        assert len(overflow) == 1
        # 溢出点 count 为被丢弃序列 count 之和
        assert overflow[0].count == 201
        assert sum(p.count for p in points) == 1200

    def test_below_limit_passes_through_unchanged(self):
        data = self._collect(lambda m: m.create_counter("c"), 5)
        capture = CaptureMetricExporter()
        bounded = setup._BoundedCardinalityMetricExporter(capture)
        bounded.export(data)
        points = capture.batches[0].resource_metrics[0].scope_metrics[0] \
            .metrics[0].data.data_points
        assert len(points) == 5
        assert not any(p.attributes.get("otel.metrics.overflow") for p in points)


# ---------------------------------------------------------------------------
# 队列满丢弃计数 + 限频安全 stderr（真实 BSP）
# ---------------------------------------------------------------------------


class BlockingSpanExporter:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def export(self, batch, timeout_millis=None, **kwargs):
        self.entered.set()
        self.release.wait(timeout=5)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis=None):
        return True

    def shutdown(self, timeout_millis=None, **kwargs):
        return True


class TestQueueFullDrop:
    def test_drop_counting_and_rate_limited_stderr(self, capsys):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BSP

        # 确定性：第一批 8 条让 worker 进入阻塞 export（队列清空），
        # 之后 12 条中前 8 条填满队列、后 4 条触发队列满丢弃。
        exporter = BlockingSpanExporter()
        bsp = _BSP(
            exporter,
            max_queue_size=8,
            max_export_batch_size=8,
            schedule_delay_millis=60000,
            export_timeout_millis=100,
        )
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(bsp)
        tracer = provider.get_tracer("queue-full-test")

        traces_state = setup._SignalState(enabled=True)
        logs_state = setup._SignalState(enabled=True)
        handler = setup._QueueFullDropHandler(traces_state, logs_state)
        sdk_logger = logging.getLogger(setup.DROP_SOURCE_LOGGER)
        sdk_logger.addHandler(handler)
        try:
            for i in range(8):
                with tracer.start_as_current_span(f"warm-{i}"):
                    pass
            assert exporter.entered.wait(timeout=5), "worker never started exporting"
            for i in range(12):
                with tracer.start_as_current_span(f"s{i}"):
                    pass
            # 队列满丢弃计数生效（worker 排空时点存在竞态，不精确断言次数）
            assert traces_state.extra["dropped_queue_full"] >= 1
            assert logs_state.extra.get("dropped_queue_full", 0) == 0
            # 限频：短时间多次队列满只向 stderr 输出一次
            first = capsys.readouterr().err
            assert "queue" in first.lower()
            for i in range(10):
                with tracer.start_as_current_span(f"late-{i}"):
                    pass
            second = capsys.readouterr().err
            assert second == ""
        finally:
            sdk_logger.removeHandler(handler)
            exporter.release.set()
            bsp.shutdown()

    def test_non_matching_records_ignored(self):
        traces_state = setup._SignalState(enabled=True)
        logs_state = setup._SignalState(enabled=True)
        handler = setup._QueueFullDropHandler(traces_state, logs_state)
        record = logging.LogRecord(
            "opentelemetry.sdk._shared_internal", logging.WARNING,
            __file__, 1, "Some other warning %s.", ("x",), None,
        )
        handler.emit(record)
        assert traces_state.extra.get("dropped_queue_full", 0) == 0
        assert logs_state.extra.get("dropped_queue_full", 0) == 0


# ---------------------------------------------------------------------------
# 关闭：先停生产者再 flush，共享 monotonic 期限
# ---------------------------------------------------------------------------


class TestShutdownBudget:
    def test_budget_order_and_drop(self, monkeypatch):
        monkeypatch.setattr(setup, "SHUTDOWN_BUDGET_SECONDS", 0.3)
        loader = make_enabled_loader()
        handle = make_handle(loader)
        order: list[str] = []
        handle.register_stop_callback(lambda: order.append("producer-stop"))

        tracer_provider = loader.set_tracer_calls[0]
        meter_provider = loader.set_meter_calls[0]
        tracer_provider.block_seconds = 2.0
        meter_provider.block_seconds = 2.0

        started = time.monotonic()
        handle.release()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"shutdown exceeded budget: {elapsed}"
        assert order == ["producer-stop"]
        with tracer_provider.event_lock:
            tracer_events = list(tracer_provider.events)
        assert tracer_events
        with meter_provider.event_lock:
            meter_events = list(meter_provider.events)
        assert meter_events == []  # 超过共享期限：丢弃
        assert handle.status == "degraded"

    def test_stop_callbacks_and_profile_recovery_within_budget(self, monkeypatch):
        # Profile 回收（最多 2 秒，T6 的 stop callback）计入共享 5 秒预算，
        # 不得让各信号累计超时，也不得无限等待线程。
        monkeypatch.setattr(setup, "SHUTDOWN_BUDGET_SECONDS", 3.0)
        loader = make_enabled_loader()
        handle = make_handle(loader)
        handle.register_stop_callback(lambda: time.sleep(2.0))  # 模拟 Profile 回收
        tracer_provider = loader.set_tracer_calls[0]
        tracer_provider.block_seconds = 10.0

        started = time.monotonic()
        handle.release()
        elapsed = time.monotonic() - started
        assert elapsed < 3.5, f"shutdown exceeded shared budget: {elapsed}"
        assert handle.snapshot()["dropped_after_deadline"] >= 1

    def test_shutdown_repeat_safe(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        handle.release()
        handle.release()

    def test_register_stop_callback_after_release_returns_false(self):
        loader = make_enabled_loader()
        handle = make_handle(loader)
        assert handle.register_stop_callback(lambda: None) is True
        handle.release()
        assert handle.register_stop_callback(lambda: None) is False
        assert len(handle._stop_callbacks) == 1


# ---------------------------------------------------------------------------
# ParentBased 采样组合
# ---------------------------------------------------------------------------


class TestSampler:
    def _decision(self, sampler, parent_context=None):
        return sampler.should_sample(
            parent_context=parent_context,
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            name="nkb.retrieval.search",
        )

    def test_no_parent_uses_ratio(self):
        sampler = setup._build_sampler(1.0)
        assert self._decision(sampler).decision.value == 2  # RECORD_AND_SAMPLE
        sampler0 = setup._build_sampler(0.0)
        assert self._decision(sampler0).decision.value == 0  # DROP

    def test_remote_sampled_parent_inherited(self):
        sampler = setup._build_sampler(0.0)
        remote_ctx = SpanContext(
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            span_id=0x1234567890ABCDEF,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        ctx = set_span_in_context(NonRecordingSpan(remote_ctx))
        assert self._decision(sampler, ctx).decision.value == 2

    def test_remote_unsampled_parent_inherited(self):
        sampler = setup._build_sampler(1.0)
        remote_ctx = SpanContext(
            trace_id=0xABCDEF0123456789ABCDEF0123456789,
            span_id=0x1234567890ABCDEF,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
        ctx = set_span_in_context(NonRecordingSpan(remote_ctx))
        assert self._decision(sampler, ctx).decision.value == 0


# ---------------------------------------------------------------------------
# 真实 SDK 子进程冒烟（env 门控；全局 Provider 只存在于子进程）
# ---------------------------------------------------------------------------


_SMOKE_SCRIPT = """
import json, time
from app.config import Settings
from app.application import telemetry_semantics as ts
from app.infrastructure.observability import setup

handle = setup.setup_observability(
    Settings(_env_file=None, otel_enabled=True),
    semantics=ts,
    semantics_keys={"outcome": ts.NKB_OUTCOME, "error_type": ts.ERROR_TYPE_ATTR},
)
assert handle is not None and handle.status == "healthy", handle.status
assert handle.snapshot()["mode"] == "local"
# 真实 SDK 下产生 span 并进入 ring buffer
with ts.retrieval_span():
    ts.record_retrieval("success", result_count=3, seconds=0.01)
snap = handle.snapshot()
names = [s["name"] for s in snap["spans"]]
assert names == [ts.RETRIEVAL_SPAN_NAME], names
assert snap["spans"][0]["outcome"] == "success", snap["spans"][0]
# exemplar：默认 trace-based，不得关闭（1.45.1 存于 _sdk_config）
from opentelemetry import metrics as otel_metrics
mp = otel_metrics.get_meter_provider()
assert type(mp._sdk_config.exemplar_filter).__name__ == "TraceBasedExemplarFilter"
# 关闭期限
t0 = time.monotonic()
handle.release()
dt = time.monotonic() - t0
assert dt < 5.0, dt
after = setup.setup_observability(Settings(_env_file=None, otel_enabled=True))
assert after is not None and after.status == "degraded", after.status
assert after.snapshot()["diagnostic_error"] == "runtime_already_shutdown"
print("SMOKE_OK", json.dumps({"spans": names, "shutdown_seconds": round(dt, 3)}))
"""


class TestRealSdkSmoke:
    def test_real_sdk_local_only_roundtrip(self):
        if not os.environ.get("NKB_OTEL_SDK_SMOKE"):
            pytest.skip("set NKB_OTEL_SDK_SMOKE=1 to run the real-SDK smoke")
        repo_root = Path(__file__).resolve().parents[3]
        proc = subprocess.run(
            [sys.executable, "-c", _SMOKE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
        )
        assert proc.returncode == 0, (
            f"real-SDK smoke failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        marker_line = next(
            line for line in proc.stdout.splitlines() if line.startswith("SMOKE_OK")
        )
        payload = json.loads(marker_line[len("SMOKE_OK"):])
        assert payload["spans"] == ["nkb.retrieval.search"]
        assert payload["shutdown_seconds"] < 5.0


def test_runtime_local_summary_and_log_buffer_release():
    from app.infrastructure.observability.logging import SafeLogBridge
    loader = make_enabled_loader()
    handle = make_handle(loader, otel_exporter_otlp_endpoint='')
    assert handle.snapshot().get('metric_snapshot', {}).get('status') == 'unknown'
    assert handle.snapshot().get('logs', {}).get('capacity') == 100
    bridge = handle.log_bridge
    bridge.emit(logging.LogRecord('test', logging.WARNING, '', 1, 'SECRET', (), None))
    assert handle.snapshot()['logs']['retained_count'] == 1
    assert 'SECRET' not in json.dumps(handle.snapshot())
    handle.release()
    assert handle.snapshot()['logs']['retained_count'] == 0


def test_summary_failure_never_skips_export():
    class BrokenSnapshot:
        def capture(self, data):
            raise RuntimeError('SECRET')
    exporter = FakeMetricExporter()
    wrapped = setup._SnapshotMetricExporter(exporter, BrokenSnapshot())
    wrapped.export(object())
    assert len(exporter.exports) == 1


def test_span_detail_attributes_use_injected_projection_and_deep_copy():
    processor = setup._RingBufferSpanProcessor({'attribute_projector':lambda attrs:{'nkb.result_count':attrs['nkb.result_count']}})
    span = SimpleNamespace(context=SimpleNamespace(trace_flags=TraceFlags(TraceFlags.SAMPLED),trace_id=1,span_id=2),
                           parent=None,status=None,name='nkb.retrieval.search',start_time=1,end_time=2,
                           attributes={'nkb.result_count':3,'query':'SECRET'})
    processor.on_end(span)
    result = processor.snapshot_spans()
    assert result[0].get('attributes') == {'nkb.result_count':3}
    result[0]['attributes']['nkb.result_count'] = 99
    assert processor.snapshot_spans()[0]['attributes']['nkb.result_count'] == 3


@pytest.mark.parametrize('endpoint', ['', _ENDPOINT])
def test_real_reader_batch_reused_for_local_and_export_summary(endpoint):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from app.application import telemetry_semantics as semantics
    loader = make_enabled_loader()
    loader.MeterProvider = MeterProvider
    loader.Resource = Resource
    readers = []
    def tracked_reader(*args, **kwargs):
        reader = PeriodicExportingMetricReader(*args, **kwargs)
        readers.append(reader)
        return reader
    loader.PeriodicExportingMetricReader = tracked_reader
    handle = setup.setup_observability(_settings(otel_enabled=True, otel_exporter_otlp_endpoint=endpoint),
                                       sdk_loader=loader, semantics=semantics)
    provider = loader.set_meter_calls[0]
    meter = provider.get_meter('test')
    meter.create_histogram(semantics.INSTRUMENT_RETRIEVAL_DURATION, unit='s').record(2.5, {'outcome':'success','query':'SECRET'})
    meter.create_counter(semantics.INSTRUMENT_RETRIEVAL_REQUESTS).add(4, {'outcome':'success'})
    assert provider.force_flush()
    snapshot = handle.snapshot()
    assert snapshot['metric_snapshot']['collection_count'] == 1
    instruments = {i['name']:i for i in snapshot['metric_snapshot']['instruments']}
    assert instruments[semantics.INSTRUMENT_RETRIEVAL_DURATION]['points'][0]['sum'] == 2.5
    assert instruments[semantics.INSTRUMENT_RETRIEVAL_REQUESTS]['points'][0]['value'] == 4
    assert 'SECRET' not in json.dumps(snapshot)
    assert len(readers) == 1
    if endpoint:
        exporter = next(item for item in FakeExporter.instances if isinstance(item, FakeMetricExporter))
        assert len(exporter.exports) == 1
        exporter.fail_exports = True
        provider.force_flush()
        assert handle.snapshot()['metric_snapshot']['collection_count'] == 2
        assert handle.snapshot()['signals']['metrics']['delivery'] == 'failed'
    else:
        assert snapshot['signals']['metrics']['local_collections'] == 1
    handle.release()


def test_shared_runtime_concurrent_release_keeps_then_clears_buffers():
    from concurrent.futures import ThreadPoolExecutor
    loader = make_enabled_loader()
    first = make_handle(loader, otel_exporter_otlp_endpoint='')
    second = make_handle(loader, otel_exporter_otlp_endpoint='')
    assert first is second
    first.log_bridge.emit(logging.LogRecord('test', logging.WARNING, '', 1, 'unregistered', (), None))
    assert first.snapshot()['logs']['retained_count'] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda h:h.release(), [first, second]))
    assert sorted(results) == [0, 1]
    snapshot = first.snapshot()
    assert snapshot['logs']['retained_count'] == 0
    assert snapshot['metric_snapshot']['instruments'] == []
    assert snapshot['spans'] == []


def test_composition_binds_application_details_policy():
    import ast
    from app import composition
    source = Path(composition.__file__).read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert any(isinstance(node.func, ast.Name) and node.func.id == "bind_details" for node in calls)


def test_details_policy_shared_reference_release():
    from app.application import telemetry_details as d
    from app.infrastructure.observability import sanitizer as facade
    facade.bind_details(configure=d.configure_details, project=d.project_details,
                        validate=d.validate_details, flatten=d.flatten_details)
    settings = _settings(otel_enabled=True, otel_exporter_otlp_endpoint=_ENDPOINT)
    loader = make_enabled_loader()
    first = setup.setup_observability(settings, sdk_loader=loader)
    second = setup.setup_observability(settings, sdk_loader=loader)
    assert first is second
    assert facade.project_details(retrieval_input={"query":"readable"})
    first.release()
    assert facade.project_details(retrieval_input={"query":"readable"})
    second.release()
    assert facade.project_details(retrieval_input={"query":"readable"}) == {}


def test_sdk_limits_are_explicit_for_fake_loader_and_final_release_unbinds_details():
    from app.application import telemetry_details
    from app.infrastructure.observability import sanitizer
    sanitizer.bind_details(configure=telemetry_details.configure_details,
        project=telemetry_details.project_details,validate=telemetry_details.validate_details,
        flatten=telemetry_details.flatten_details)
    loader = make_enabled_loader()
    handle = make_handle(loader,otel_content_max_bytes=65536)
    span_limits = loader.set_tracer_calls[0].kwargs['span_limits']
    log_limits = loader.set_logger_calls[0].kwargs['log_record_limits']
    assert span_limits.max_span_attributes >= 100
    assert span_limits.max_event_attributes >= 100
    assert span_limits.max_span_attribute_length >= 65536
    assert log_limits.max_log_record_attribute_length >= 65536
    handle.release()
    assert sanitizer._callbacks is None
