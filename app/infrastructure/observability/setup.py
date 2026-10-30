"""OTel SDK 装配与运行时句柄（Infrastructure 层，T4 of plan-261029）。

本模块是 N-KB 四信号（Trace/Metric/Log/Profile）Provider/Exporter 的唯一
装配点，移植 N-Agent 已验证的装配模式（P059 发布缓存、P057 kwargs 核验），
并按 spec-261029 调整：

- ``setup_observability(settings)``：开关关闭返回 ``None`` 且零 SDK import；
  开启后延迟 import SDK/exporter。统一 Resource：``service.name=n-kb``、
  项目版本、每进程唯一 ``service.instance.id``。
- 进程锁保护发布与引用计数：比较全部影响运行时的配置（含 content 限额与
  诊断外链），同配置共享运行时并引用计数；冲突返回独立 degraded 诊断句柄，
  不 acquire 原运行时、不改写原使用者配置。已发布过的全局 Provider 关闭后
  不可复活（重启用新进程）。外部已安装的 Provider 不覆盖、不关闭，该信号
  降级并诊断。
- Trace/Metric/Log 三条独立有界 pipeline + Profile 状态。采样为
  ``ParentBased(TraceIdRatioBased(ratio))``；Trace/Log 队列 2048/批 512、
  导出超时 3 秒；Metric 周期 15 秒；exemplar 保持 trace-based 默认（P062）。
- Metric cardinality：已核验 SDK 1.45.1 无原生 cardinality limit / overflow
  支持（P057 源码核验），由 ``_BoundedCardinalityMetricExporter`` 在导出侧
  真实兜底——每个 instrument 每次收集最多 1000 个数据点（含 1 个
  ``otel.metrics.overflow`` 溢出点，承载被丢弃序列的总量）。
- 队列满：SDK 在 ``opentelemetry.sdk._shared_internal`` logger 上输出
  "Queue full, dropping %s."（1.45.1 源码核验）；本模块挂接计数 + 限频安全
  stderr handler，只统计自己的队列丢弃，release 时移除。
- local-only（无 OTLP endpoint）不创建任何网络 exporter；Trace 保留最近
  100 条已结束 sampled span 的安全摘要；Metric 有界聚合；Log 仅 stderr。
  无出口为 ``mode=local``，任一出口为 ``mode=export``；mode 不替代 status。
- release：引用计数归零才结束；先停业务生产者/Profile，再 flush/shutdown；
  所有信号共享一个 monotonic 5 秒截止时间（Profile 最多 2 秒回收包含在
  预算内，不叠加）；超时丢弃并计数，不无限等待线程；只解除本模块拥有的
  handler。
- 每信号记录尝试/成功/失败次数与最近成功/失败时间，网络 delivery 初始
  ``unknown``；初始化失败、最近导出失败或 Profile 停用使 status=degraded，
  后续成功恢复（累计失败次数不重置）；禁用信号不参与聚合。成功只表示本
  进程交付确认。

只 import ``app.config``、本包 ``sanitizer`` 与 OTel SDK/exporter；禁止
import ``app.application``（telemetry_semantics 经组合根以 ``semantics`` /
``semantics_keys`` 注入）、``app.domain``、``app.interfaces``。
"""
from __future__ import annotations

import logging
import copy
import sys
import threading
import time
import uuid
from collections import deque
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional

from app.config import Settings

from .sanitizer import configure_details
from .snapshots import MetricSnapshot, SafeLogBuffer

SERVICE_NAME = "n-kb"
UNKNOWN_VERSION = "unknown"

SHUTDOWN_THREAD_NAME = "n-kb-observability-shutdown"

# 有界投递预算（spec：Trace/Log 队列 2048、批 512、OTLP 超时 3 秒、Metric
# 周期 15 秒）。
SPAN_QUEUE_MAX = 2048
LOG_QUEUE_MAX = 2048
EXPORT_BATCH_MAX = 512
OTLP_TIMEOUT_SECONDS = 3.0
METRIC_EXPORT_PERIOD_SECONDS = 15.0

# 全部信号共享的关闭总预算；Profile 最多 2 秒的回收包含在其中（不叠加）。
SHUTDOWN_BUDGET_SECONDS = 5.0
PROFILE_RECOVERY_SECONDS = 2.0

# 快照 ring buffer 与投影上限。
RING_BUFFER_MAX = 100
SNAPSHOT_FIELD_CHAR_CAP = 256
LOCAL_METRIC_NAMES_MAX = 1000

# Metric cardinality 兜底（SDK 1.45.1 无原生支持，P057 核验）：
# 每 instrument 每次收集最多 1000 个数据点，含 1 个 overflow 溢出点。
METRIC_CARDINALITY_LIMIT = 1000
OVERFLOW_ATTRIBUTE = "otel.metrics.overflow"

# SDK 队列满丢弃的公开诊断信号（1.45.1 源码核验：BatchProcessor.emit 在
# 该 logger 上输出 "Queue full, dropping %s."，arg 为 "Span"/"LogRecord"）。
DROP_SOURCE_LOGGER = "opentelemetry.sdk._shared_internal"
DROP_MESSAGE = "Queue full, dropping %s."
DROP_STDERR_INTERVAL_SECONDS = 30.0

_STATUS_DISABLED = "disabled"
_STATUS_HEALTHY = "healthy"
_STATUS_DEGRADED = "degraded"

_DELIVERY_DISABLED = "disabled"
_DELIVERY_UNKNOWN = "unknown"
_DELIVERY_SUCCEEDED = "succeeded"
_DELIVERY_FAILED = "failed"

# 影响运行时的全部配置（spec：包括 content 限额及诊断外链）。
_RUNTIME_CONFIG_FIELDS = (
    "otel_exporter_otlp_endpoint",
    "otel_traces_sample_ratio",
    "otel_content_max_bytes",
    "otel_grafana_base_url",
    "otel_grafana_traces_base_url",
    "otel_grafana_metrics_base_url",
    "otel_grafana_logs_base_url",
    "otel_grafana_profile_base_url",
    "profiling_enabled",
    "profiling_pyroscope_endpoint",
    "profiling_interval_seconds",
)


def _service_version() -> str:
    try:
        from importlib.metadata import version

        return version(SERVICE_NAME)
    except Exception:
        return UNKNOWN_VERSION


def _now() -> float:
    return time.monotonic()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = SNAPSHOT_FIELD_CHAR_CAP) -> str:
    return value if len(value) <= limit else value[:limit]


# ---------------------------------------------------------------------------
# 每信号状态（尝试/成功/失败次数与最近时间；成功恢复 current 故障）
# ---------------------------------------------------------------------------


class _SignalState:
    """单信号（traces/metrics/logs/profile）的运行时状态。"""

    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.status: str = _STATUS_HEALTHY if enabled else _STATUS_DISABLED
        self.delivery: Optional[str] = None  # None -> enabled 后呈 unknown
        self.attempt_count: int = 0
        self.success_count: int = 0
        self.failure_count: int = 0
        self.last_success: Optional[str] = None
        self.last_failure: Optional[str] = None
        self.current_error: Optional[str] = None
        self.extra: Dict[str, Any] = {}

    def degrade(self, error_code: str) -> None:
        if not self.enabled:
            return
        self.status = _STATUS_DEGRADED
        self.current_error = error_code

    def record_export_success(self) -> None:
        self.delivery = _DELIVERY_SUCCEEDED
        self.success_count += 1
        self.last_success = _utc_now_iso()
        # 成功只清除 current 传输故障并恢复状态；累计失败次数永不重置。
        if self.current_error and self.current_error.startswith("export_"):
            self.current_error = None
            self.status = _STATUS_HEALTHY

    def record_export_failure(self, error_code: str = "export_failed") -> None:
        self.delivery = _DELIVERY_FAILED
        self.failure_count += 1
        self.last_failure = _utc_now_iso()
        self.current_error = error_code
        self.status = _STATUS_DEGRADED

    def as_json(self) -> Dict[str, Any]:
        delivery = _DELIVERY_DISABLED
        if self.enabled:
            delivery = self.delivery or _DELIVERY_UNKNOWN
        payload = {
            "status": self.status,
            "delivery": delivery,
            "attempt_count": self.attempt_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "current_error": self.current_error,
        }
        payload.update(self.extra)
        return payload


def _export_failed(result: Any) -> bool:
    """解释 exporter 的公开返回值（不读 SDK 私有字段）。"""
    if isinstance(result, BaseException):
        return True
    name = getattr(result, "name", None)
    if name is not None:
        return name != "SUCCESS"
    return not result


class _DeliveryStatusWrapper:
    """包装真实 exporter，仅依据公开 ``export()`` 结果维护单信号投递状态。"""

    def __init__(self, exporter: Any, signal: _SignalState):
        self.exporter = exporter
        self.signal = signal

    def __getattr__(self, name: str) -> Any:
        # 委托 SDK 侧属性（如 PeriodicExportingMetricReader 读取的
        # _preferred_temporality）到被包装 exporter。
        return getattr(self.exporter, name)

    def export(self, data: Any, *args: Any, **kwargs: Any) -> Any:
        self.signal.attempt_count += 1
        try:
            result = self.exporter.export(data, *args, **kwargs)
        except Exception:
            # exporter 异常不进入业务路径；记一次投递失败并返回失败值。
            self.signal.record_export_failure()
            return None
        if _export_failed(result):
            self.signal.record_export_failure()
        else:
            self.signal.record_export_success()
        return result

    def force_flush(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.exporter.force_flush(*args, **kwargs)
        except Exception:
            # 投递侧失败只记录，绝不传播进业务/SDK 关闭路径。
            self.signal.record_export_failure("export_flush_failed")
            return False

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.exporter.shutdown(*args, **kwargs)
        except Exception:
            self.signal.record_export_failure("export_shutdown_failed")
            return None


# ---------------------------------------------------------------------------
# Ring buffer span processor（仅已结束 + sampled；安全字段投影）
# ---------------------------------------------------------------------------

# 快照字段为固定枚举集合：任何 span 属性原文（含白名单键值）都不进入
# 快照；outcome/error_type 只在组合根注入语义键时按 bounded 枚举提取。
_SNAPSHOT_KEYS = (
    "name",
    "trace_id",
    "span_id",
    "parent_span_id",
    "status",
    "outcome",
    "error_type",
    "duration_ms",
)


class _RingBufferSpanProcessor:
    """保留最近 N 条已结束、sampled span 的有界安全投影。

    不保存完整 Span/事件/异常/Resource；字符串限长；属性仅按组合根注入
    的语义键提取 bounded 枚举（outcome / error.type），无注入则不提取。
    """

    def __init__(self, semantics_keys: Optional[Mapping[str, str]] = None):
        keys = dict(semantics_keys or {})
        self._outcome_key = keys.get("outcome")
        self._error_type_key = keys.get("error_type")
        self._attribute_projector = keys.get("attribute_projector")
        self._closed = False
        self._lock = threading.Lock()
        self._buffer: deque = deque(maxlen=RING_BUFFER_MAX)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # 只缓存已结束 span；on_start 永不记录。
        return None

    def _on_ending(self, span: Any) -> None:
        # SDK 1.45 SpanProcessor 合同：end() 时在 on_end 前调用，绝不抛出。
        return None

    def on_end(self, span: Any) -> None:
        try:
            context = getattr(span, "context", None)
            if context is None:
                return
            if not context.trace_flags.sampled:
                return
            trace_id = getattr(context, "trace_id", None)
            span_id = getattr(context, "span_id", None)
            if not trace_id or not span_id:
                return
            parent = getattr(span, "parent", None)
            parent_span_id = getattr(parent, "span_id", None) if parent else None
            status_obj = getattr(span, "status", None)
            status_code = getattr(status_obj, "status_code", status_obj)
            attributes = getattr(span, "attributes", None) or {}
            entry = {
                "name": _bounded_text(getattr(span, "name", "") or ""),
                "trace_id": format(trace_id, "032x"),
                "span_id": format(span_id, "016x"),
                "parent_span_id": (
                    format(parent_span_id, "016x") if parent_span_id else None
                ),
                "status": _bounded_text(
                    getattr(status_code, "name", str(status_code))
                    if status_code is not None
                    else ""
                ),
                "outcome": self._bounded_attr(attributes, self._outcome_key),
                "error_type": self._bounded_attr(attributes, self._error_type_key),
                "duration_ms": self._duration_ms(span),
            }
            if self._attribute_projector is not None:
                from .snapshots import project_span_facts
                entry["attributes"] = project_span_facts(self._attribute_projector(attributes))
        except Exception:
            return
        with self._lock:
            if not self._closed:
                self._buffer.append(entry)

    @staticmethod
    def _bounded_attr(attributes: Mapping[str, Any], key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        value = attributes.get(key)
        if isinstance(value, str) and value:
            return value[:SNAPSHOT_FIELD_CHAR_CAP]
        return None

    @staticmethod
    def _duration_ms(span: Any) -> Optional[float]:
        try:
            start = span.start_time
            end = span.end_time
            if start is None or end is None:
                return None
            return round((end - start) / 1_000_000, 3)
        except Exception:
            return None

    def snapshot_spans(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._buffer))

    def force_flush(self, timeout_millis: Optional[float] = None) -> bool:
        return True

    def shutdown(self, timeout_millis: Optional[float] = None) -> bool:
        with self._lock:
            self._closed = True
            self._buffer.clear()
        return True


# ---------------------------------------------------------------------------
# Metric cardinality 兜底（SDK 1.45.1 无原生 cardinality limit / overflow）
# ---------------------------------------------------------------------------


class _SnapshotMetricExporter:
    """Capture the existing reader batch before delivery; isolate diagnostics."""

    def __init__(self, inner, snapshot):
        self._inner, self._snapshot = inner, snapshot

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def export(self, data, *args, **kwargs):
        try:
            self._snapshot.capture(data)
        except Exception:
            pass
        return self._inner.export(data, *args, **kwargs)


class _BoundedCardinalityMetricExporter:
    """导出侧 cardinality 兜底：每个 instrument 每次收集最多
    ``METRIC_CARDINALITY_LIMIT`` 个数据点（含 1 个 overflow 溢出点）。

    溢出点承载被丢弃序列的总量（Sum 合并 value、Histogram 合并 count/sum），
    保证总量不丢、序列数有界；未超限时原数据对象原样透传。"""

    def __init__(self, inner: Any):
        # MetricExporter.__init__ 设置 _preferred_temporality/_preferred_
        # aggregation（PeriodicExportingMetricReader 读取）。延迟 import，
        # 禁用路径永不加载 SDK。
        from opentelemetry.sdk.metrics.export import MetricExporter

        MetricExporter.__init__(self)
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def export(self, metrics_data: Any, *args: Any, **kwargs: Any) -> Any:
        bounded = self._bound_metrics_data(metrics_data)
        return self._inner.export(bounded, *args, **kwargs)

    def force_flush(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.force_flush(*args, **kwargs)

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.shutdown(*args, **kwargs)

    def _bound_metrics_data(self, metrics_data: Any) -> Any:
        try:
            # MetricsData/ResourceMetrics/ScopeMetrics/Metric 均为 frozen
            # dataclass（1.45.1 核验）：仅重建受影响的不可变链路。
            from dataclasses import replace

            changed = False
            new_resource_metrics = []
            for rm in getattr(metrics_data, "resource_metrics", ()):
                new_scope_metrics = []
                rm_changed = False
                for sm in getattr(rm, "scope_metrics", ()):
                    new_metrics = []
                    sm_changed = False
                    for metric in getattr(sm, "metrics", ()):
                        data = getattr(metric, "data", None)
                        points = getattr(data, "data_points", None)
                        if points is None or len(points) <= METRIC_CARDINALITY_LIMIT:
                            new_metrics.append(metric)
                            continue
                        new_metrics.append(
                            replace(metric, data=self._with_overflow(data, points))
                        )
                        sm_changed = True
                    if sm_changed:
                        new_scope_metrics.append(replace(sm, metrics=new_metrics))
                        rm_changed = True
                    else:
                        new_scope_metrics.append(sm)
                if rm_changed:
                    new_resource_metrics.append(
                        replace(rm, scope_metrics=new_scope_metrics)
                    )
                    changed = True
                else:
                    new_resource_metrics.append(rm)
            if changed:
                return replace(metrics_data, resource_metrics=new_resource_metrics)
            return metrics_data
        except Exception:
            # 兜底自身失败时不阻断导出：原样透传（可能超限，但不丢数据）。
            return metrics_data

    def _with_overflow(self, data: Any, points: Any) -> Any:
        """构造截断后的数据对象：保留前 N-1 个点 + 1 个溢出点。"""
        kept = list(points[: METRIC_CARDINALITY_LIMIT - 1])
        dropped = list(points[METRIC_CARDINALITY_LIMIT - 1:])
        overflow = self._build_overflow_point(data, kept, dropped)
        if overflow is None:
            return data
        new_points = kept + [overflow]
        return self._rebuild_data(data, new_points)

    @staticmethod
    def _build_overflow_point(data: Any, kept: list, dropped: list) -> Any:
        from opentelemetry.sdk.metrics.export import (
            HistogramDataPoint,
            NumberDataPoint,
        )

        first = dropped[0]
        last = dropped[-1]
        if type(data).__name__ == "Histogram":
            return HistogramDataPoint(
                attributes={OVERFLOW_ATTRIBUTE: True},
                start_time_unix_nano=getattr(first, "start_time_unix_nano", 0),
                time_unix_nano=getattr(last, "time_unix_nano", 0),
                count=sum(getattr(p, "count", 0) for p in dropped),
                sum=sum(getattr(p, "sum", 0) or 0 for p in dropped),
                bucket_counts=(),
                explicit_bounds=(),
                min=0,
                max=0,
            )
        if type(data).__name__ == "Sum":
            return NumberDataPoint(
                attributes={OVERFLOW_ATTRIBUTE: True},
                start_time_unix_nano=getattr(first, "start_time_unix_nano", 0),
                time_unix_nano=getattr(last, "time_unix_nano", 0),
                value=sum(getattr(p, "value", 0) for p in dropped),
            )
        if type(data).__name__ == "Gauge":
            return NumberDataPoint(
                attributes={OVERFLOW_ATTRIBUTE: True},
                start_time_unix_nano=getattr(first, "start_time_unix_nano", 0),
                time_unix_nano=getattr(last, "time_unix_nano", 0),
                value=0,
            )
        return None

    @staticmethod
    def _rebuild_data(data: Any, new_points: list) -> Any:
        name = type(data).__name__
        if name == "Sum":
            return type(data)(
                new_points,
                data.aggregation_temporality,
                data.is_monotonic,
            )
        if name == "Histogram":
            return type(data)(new_points, data.aggregation_temporality)
        if name == "Gauge":
            return type(data)(new_points)
        return data


# ---------------------------------------------------------------------------
# local-only 有界 metric 聚合
# ---------------------------------------------------------------------------


class _BoundedLocalMetricExporter:
    """仅保留最近一次有界聚合快照（instrument 名称与收集次数）。"""

    def __init__(self):
        from opentelemetry.sdk.metrics.export import MetricExporter

        MetricExporter.__init__(self)
        self._lock = threading.Lock()
        self._latest_names: List[str] = []
        self.collect_count = 0

    def export(self, metrics_data: Any, *args: Any, **kwargs: Any) -> Any:
        names: List[str] = []
        try:
            for resource_metrics in getattr(metrics_data, "resource_metrics", ()):
                for scope_metrics in getattr(resource_metrics, "scope_metrics", ()):
                    for metric in getattr(scope_metrics, "metrics", ()):
                        name = getattr(metric, "name", None)
                        if isinstance(name, str):
                            names.append(name[:SNAPSHOT_FIELD_CHAR_CAP])
        except Exception:
            names = []
        with self._lock:
            self._latest_names = names[:LOCAL_METRIC_NAMES_MAX]
            self.collect_count += 1
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: Optional[float] = None, **kwargs: Any) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis: Optional[float] = None, **kwargs: Any) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        return MetricExportResult.SUCCESS

    def latest(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "local_instruments": list(self._latest_names),
                "local_collections": self.collect_count,
            }


# ---------------------------------------------------------------------------
# 队列满丢弃计数 + 限频安全 stderr（挂接在 SDK 自己的 logger 上）
# ---------------------------------------------------------------------------


class _QueueFullDropHandler(logging.Handler):
    """统计 SDK BatchProcessor 的队列满丢弃（公开日志信号，P057 核验）。

    只读取 message 为 "Queue full, dropping %s."（arg 为 "Span"/
    "LogRecord"）的记录；计数进入对应信号 extra，并以限频方式向 stderr
    输出一行安全摘要（无原文、无限重试）。本 handler 归本模块所有，
    release 时移除，不改写既有 handler。"""

    def __init__(
        self,
        span_state: _SignalState,
        log_state: _SignalState,
        stderr_interval: float = DROP_STDERR_INTERVAL_SECONDS,
    ):
        super().__init__(level=logging.WARNING)
        self._span_state = span_state
        self._log_state = log_state
        self._stderr_interval = stderr_interval
        self._lock = threading.Lock()
        self._last_stderr = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.getMessage() != "Queue full, dropping Span." and (
                record.getMessage() != "Queue full, dropping LogRecord."
            ):
                return
            state = (
                self._span_state
                if record.args and record.args[0] == "Span"
                else self._log_state
            )
            if state is None or not state.enabled:
                return
            state.extra["dropped_queue_full"] = (
                state.extra.get("dropped_queue_full", 0) + 1
            )
            self._emit_stderr_rate_limited(state)
        except Exception:
            return

    def _emit_stderr_rate_limited(self, state: _SignalState) -> None:
        now = _now()
        with self._lock:
            if now - self._last_stderr < self._stderr_interval:
                return
            self._last_stderr = now
        try:
            # 限频安全 stderr：只输出计数与信号名，不输出任何遥测内容。
            sys.stderr.write(
                "n-kb observability: %s export queue full, dropped=%d\n"
                % (
                    "traces" if state is self._span_state else "logs",
                    state.extra.get("dropped_queue_full", 0),
                )
            )
            sys.stderr.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 采样器
# ---------------------------------------------------------------------------


def _build_sampler(ratio: float):
    """ParentBased(TraceIdRatioBased(ratio))：ratio 只约束无父本地根；
    父决策（含 remote）一律继承。"""
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    return ParentBased(TraceIdRatioBased(ratio))


def _trace_based_exemplar_filter():
    """P062：exemplar 保持默认 trace-based（度量记录发生在对应操作 span
    仍为 current 时）。已核验 1.45.1：MeterProvider(exemplar_filter=None)
    默认即 trace-based，显式传入以防默认变更。"""
    from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter

    return TraceBasedExemplarFilter()


def _detail_sdk_limits(max_bytes):
    """Explicit SDK limits accommodate all eleven envelopes plus fixed facts."""
    from opentelemetry.sdk.trace import SpanLimits
    from opentelemetry.sdk._logs import LogRecordLimits
    count = 256
    max_bytes = max(512, max_bytes)  # fixed HTTP/identity facts also need room
    return (SpanLimits(max_attributes=count, max_span_attributes=count,
                       max_event_attributes=count, max_attribute_length=max_bytes,
                       max_span_attribute_length=max_bytes),
            LogRecordLimits(max_attributes=count, max_attribute_length=max_bytes))


# ---------------------------------------------------------------------------
# 真实 SDK loader（懒加载；禁用路径永不调用）
# ---------------------------------------------------------------------------


def _default_sdk_loader() -> SimpleNamespace:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from .instrumentors import instrument_system_metrics
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    import opentelemetry.trace as otel_trace
    import opentelemetry.metrics as otel_metrics
    import opentelemetry._logs as otel_logs

    _proxy_names = {
        "ProxyTracerProvider",
        "_ProxyMeterProvider",
        "ProxyLoggerProvider",
    }

    def _is_unclaimed(provider: Any) -> bool:
        return provider is None or type(provider).__name__ in _proxy_names

    return SimpleNamespace(
        Resource=Resource,
        TracerProvider=TracerProvider,
        MeterProvider=MeterProvider,
        LoggerProvider=LoggerProvider,
        LoggingHandler=LoggingHandler,
        instrument_system_metrics=instrument_system_metrics,
        BatchSpanProcessor=BatchSpanProcessor,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        PeriodicExportingMetricReader=PeriodicExportingMetricReader,
        TraceBasedExemplarFilter=TraceBasedExemplarFilter,
        OTLPSpanExporter=OTLPSpanExporter,
        OTLPMetricExporter=OTLPMetricExporter,
        OTLPLogExporter=OTLPLogExporter,
        set_tracer_provider=otel_trace.set_tracer_provider,
        get_tracer_provider=otel_trace.get_tracer_provider,
        set_meter_provider=otel_metrics.set_meter_provider,
        get_meter_provider=otel_metrics.get_meter_provider,
        set_logger_provider=otel_logs.set_logger_provider,
        get_logger_provider=otel_logs.get_logger_provider,
        is_unclaimed=_is_unclaimed,
    )


# ---------------------------------------------------------------------------
# 运行时句柄
# ---------------------------------------------------------------------------


def _metric_views(semantics):
    from opentelemetry.sdk.metrics.view import View, ExplicitBucketHistogramAggregation
    return [View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(boundaries=spec.boundaries))
            for name, spec in getattr(semantics, "INSTRUMENTS", {}).items()
            if getattr(spec, "kind", None) == "histogram" and spec.boundaries]


class ObservabilityHandle:
    """运行时句柄：纯 JSON ``snapshot()``、预算内 ``release()``。

    Interfaces 只消费注入的 snapshot callable，不接触 SDK。"""

    def __init__(
        self,
        *,
        mode: str,
        instance_id: str,
        sample_ratio: float,
        signals: Dict[str, _SignalState],
        ring_buffer: Optional[_RingBufferSpanProcessor],
        providers: List[Any],
        local_metric_exporter: Optional[_BoundedLocalMetricExporter],
        semantics: Any = None,
        diagnostic_error: Optional[str] = None,
        differing_fields: Optional[List[str]] = None,
    ):
        self._mode = mode
        self._instance_id = instance_id
        self._sample_ratio = sample_ratio
        self._signals = signals
        self._ring_buffer = ring_buffer
        self._providers = providers
        self._local_metric_exporter = local_metric_exporter
        self._metric_snapshot = MetricSnapshot(getattr(semantics, "INSTRUMENTS", {}))
        self._log_buffer = SafeLogBuffer()
        self.log_bridge = None
        # 语义注册表（提供 SEMCONV_VERSION / INSTRUMENTS 的模块型对象）；
        # 由组合根注入，本模块不反向 import Application。
        self._semantics = semantics
        self._diagnostic_error = diagnostic_error
        self._differing_fields = list(differing_fields or [])
        self._drop_handler: Optional[_QueueFullDropHandler] = None
        self._stop_callbacks: List[Callable[[], None]] = []
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._dropped_after_deadline = 0
        # 全局 Provider 已发布（不可逆）的标记：这类句柄即使 degraded 也
        # 必须被缓存，避免重试重复装配（泄漏）。
        self._published = False
        # 共享运行时的归属（诊断句柄为 None：release 不影响共享运行时）。
        self._runtime: Optional["_SharedRuntime"] = None
        self.status = self._compute_status()

    # -- 生命周期 -----------------------------------------------------------

    @property
    def is_published(self) -> bool:
        """Whether setup published this runtime, rather than a diagnostic handle."""
        return self._published

    @property
    def mode(self) -> str:
        """出口模式：``local``（无 OTLP 出口）/ ``export``（任一出口）。
        mode 不替代 status。"""
        return self._mode

    @property
    def instance_id(self) -> str:
        """每进程唯一的 service.instance.id。"""
        return self._instance_id

    def register_stop_callback(self, callback: Callable[[], None]) -> bool:
        """注册业务生产者（含 Profile worker）的停止回调，在 flush/shutdown
        之前调用。已 shutdown 后注册返回 False（迟到者无法再被停止），
        调用方不得假定注册成功。"""
        with self._shutdown_lock:
            if self._shutdown_done:
                return False
            self._stop_callbacks.append(callback)
            return True

    def release(self) -> Optional[int]:
        """引用计数释放：最后一个使用者退出才结束运行时。

        返回释放后的剩余引用数（0 表示已执行 shutdown）；诊断句柄不持有
        共享运行时，release 无效果并返回 None。"""
        global _RUNTIME
        with _STATE_LOCK:
            runtime = self._runtime
            if runtime is None or runtime.handle is not self:
                return None
            if self._shutdown_done:
                return None
            if runtime.refcount > 1:
                runtime.refcount -= 1
                return runtime.refcount
            _RUNTIME = None
        self.shutdown()
        self._finalize_release()
        return 0

    def shutdown(self) -> None:
        """一次性、幂等的关闭：先停生产者/Profile，再以剩余预算
        flush/shutdown 所有 provider；全部信号共享一个 monotonic 截止
        时间（默认 5 秒，Profile 回收包含在内）。阻塞的 SDK 清理在 worker
        线程执行，超时后丢弃剩余数据（计数），不无限等待线程。"""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        deadline = _now() + SHUTDOWN_BUDGET_SECONDS
        # 1. 先停业务生产者/Profile（本模块拥有，快速同步；Profile 最多
        #    2 秒回收由 T6 的 stop callback 承担，包含在本预算内）。
        #    信任边界：callback 本身在本线程同步执行、无 watchdog，release()
        #    的阻塞时长为 max(回调总耗时, SHUTDOWN_BUDGET_SECONDS)；T6 的
        #    stop callback 契约必须自限 2 秒，新增 callback 同样不得阻塞。
        for callback in list(self._stop_callbacks):
            try:
                callback()
            except Exception:
                pass
        # 2. flush/shutdown providers（剩余预算，worker 线程执行）。
        dropped = 0
        if self._providers:

            def _drain() -> None:
                nonlocal dropped
                for provider in self._providers:
                    remaining = deadline - _now()
                    if remaining <= 0:
                        dropped += 1
                        continue
                    try:
                        provider.force_flush(timeout_millis=int(remaining * 1000))
                        provider.shutdown()
                    except Exception:
                        dropped += 1

            worker = threading.Thread(
                target=_drain, name=SHUTDOWN_THREAD_NAME, daemon=True
            )
            worker.start()
            worker.join(max(0.0, deadline - _now()))
            if worker.is_alive():
                # 到期：剩余数据丢弃，仅保留安全计数；daemon 线程自行结束。
                dropped += 1
        self._dropped_after_deadline = dropped
        self.status = self._compute_status()

    def _finalize_release(self) -> None:
        """最后一次释放的收尾：解除本模块拥有的 drop handler，并将 details
        投影恢复为 fail-closed（sanitizer=None）。"""
        from .logging import detach_observability_log_handlers, configure_execution_context
        detach_observability_log_handlers()
        configure_execution_context()
        self._log_buffer.clear()
        self._metric_snapshot.clear()
        if self._ring_buffer is not None:
            self._ring_buffer.shutdown()
        handler = self._drop_handler
        if handler is not None:
            self._drop_handler = None
            try:
                logging.getLogger(DROP_SOURCE_LOGGER).removeHandler(handler)
            except Exception:
                pass
        try:
            configure_details(None)
        except Exception:
            pass
        from .sanitizer import clear_details_bindings
        clear_details_bindings()

    # -- Profile 状态桥接 ----------------------------------------------------

    def mark_profiler_attached(self) -> bool:
        """组合根成功启动 Profile worker 后清除装配期
        ``profiler_not_configured`` 故障；delivery 保持 unknown 直到首个
        窗口实际交付。"""
        state = self._signals.get("profile")
        if state is None or not state.enabled:
            return False
        if state.current_error == "profiler_not_configured":
            state.current_error = None
            state.status = _STATUS_HEALTHY
        self.status = self._compute_status()
        return True

    def profiler_on_status(self) -> Callable[[str, Optional[str]], None]:
        """桥接 ``ProfilingSampler.on_status(status, code)``（公开的按窗口
        结果、仅安全错误码）到 profile 信号状态：healthy 清除 current 故障，
        degraded 记失败计数与错误码（含三振出局 ``sampler_disabled``）。"""

        def _bridge(status: str, code: Optional[str]) -> None:
            state = self._signals.get("profile")
            if state is None or not state.enabled:
                return
            if status == "healthy":
                state.delivery = _DELIVERY_SUCCEEDED
                state.last_success = _utc_now_iso()
                state.current_error = None
                state.status = _STATUS_HEALTHY
            elif status == "degraded":
                state.delivery = _DELIVERY_FAILED
                state.last_failure = _utc_now_iso()
                state.failure_count += 1
                state.current_error = code or "profiler_failed"
                state.status = _STATUS_DEGRADED
            self.status = self._compute_status()

        return _bridge

    # -- 快照 ----------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """纯 JSON 四信号快照：加锁读取、返回副本、不保留原记录。"""
        partial_failure = False
        spans: List[Dict[str, Any]] = []
        if self._ring_buffer is not None:
            try:
                spans = self._ring_buffer.snapshot_spans()
            except Exception:
                # 读取失败保留可读部分并报告 degraded；绝不伪造 healthy。
                partial_failure = True
        # extra 由导出线程并发插入；首次插入扩容可能与 update 迭代竞态，
        # 单信号失败不拖垮整体快照（与 partial_failure 模式一致）。
        signals: Dict[str, Any] = {}
        for name, state in self._signals.items():
            try:
                signals[name] = state.as_json()
            except Exception:
                signals[name] = {"status": _STATUS_DEGRADED, "delivery": _DELIVERY_UNKNOWN}
                partial_failure = True
        if self._local_metric_exporter is not None:
            try:
                signals["metrics"].update(self._local_metric_exporter.latest())
            except Exception:
                partial_failure = True
        try:
            metric_summary = self._metric_snapshot.latest()
        except Exception:
            metric_summary = {"status": "degraded", "error": "metric_snapshot_failed", "instruments": []}
            partial_failure = True
        try:
            log_summary = self._log_buffer.latest()
        except Exception:
            log_summary = {"status": "degraded", "items": [], "retained_count": 0, "capacity": 100, "returned_count": 0, "evicted_count": 0}
            partial_failure = True
        status = self._compute_status()
        if partial_failure or (self._shutdown_done and self._mode != "local"):
            if status != _STATUS_DISABLED:
                status = _STATUS_DEGRADED
        snapshot: Dict[str, Any] = {
            "status": status,
            "mode": self._mode,
            "instance_id": self._instance_id,
            "sample_ratio": self._sample_ratio,
            "spans": spans,
            "metric_snapshot": metric_summary,
            "logs": log_summary,
            "signals": signals,
            "dropped_after_deadline": self._dropped_after_deadline,
            "diagnostic_error": self._diagnostic_error,
            "differing_fields": list(self._differing_fields),
        }
        if self._semantics is not None:
            snapshot["semconv_version"] = getattr(
                self._semantics, "SEMCONV_VERSION", None
            )
            snapshot["instruments"] = [
                {
                    "name": name,
                    "kind": getattr(inst, "kind", None),
                    "unit": getattr(inst, "unit", None),
                    "allowed_dimensions": list(
                        getattr(inst, "allowed_dimensions", ()) or ()
                    ),
                }
                for name, inst in getattr(self._semantics, "INSTRUMENTS", {}).items()
            ]
        return snapshot

    def _compute_status(self) -> str:
        for state in self._signals.values():
            # 禁用信号不参与聚合。
            if state.enabled and state.status == _STATUS_DEGRADED:
                return _STATUS_DEGRADED
        if self._dropped_after_deadline:
            return _STATUS_DEGRADED
        return _STATUS_HEALTHY


# ---------------------------------------------------------------------------
# 进程级发布缓存与引用计数（P059）
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()


class _SharedRuntime:
    __slots__ = ("config", "handle", "refcount")

    def __init__(self, config: Dict[str, Any], handle: ObservabilityHandle):
        self.config = config
        self.handle = handle
        self.refcount = 1


_RUNTIME: Optional[_SharedRuntime] = None
_PUBLISHED_ONCE = False


def _runtime_config(settings: Settings) -> Dict[str, Any]:
    return {field: getattr(settings, field) for field in _RUNTIME_CONFIG_FIELDS}


def _resolve_mode(settings: Settings, endpoint: str) -> str:
    """mode 语义（spec 111）：任一 OTLP 或 Profile 网络出口启用为 export。

    仅 OTLP endpoint 为空但 Profile 启用且自带 endpoint 时同样是
    ``export``（存在网络上传出口），不得误报 local。
    """
    has_profile_outlet = bool(
        settings.profiling_enabled and settings.profiling_pyroscope_endpoint.strip()
    )
    return "export" if (endpoint or has_profile_outlet) else "local"


def setup_observability(
    settings: Settings,
    *,
    sdk_loader: Any = None,
    semantics: Any = None,
    semantics_keys: Optional[Mapping[str, str]] = None,
) -> Optional[ObservabilityHandle]:
    """装配 OTel 运行时（进程内幂等 + 引用计数），返回其句柄。

    - 开关关闭：返回 ``None``，零 SDK import，不装配任何组件。
    - 同配置重复调用：共享运行时，引用计数加一。
    - 配置冲突：返回独立 degraded 诊断句柄（不改写原使用者配置，不
      acquire 原运行时）。
    - 已发布并关闭过：全局 Provider 不可复活，返回独立 degraded 诊断
      句柄（重启请用新进程）。
    - 部分初始化失败：只清理本模块拥有的资源，返回 degraded 句柄，
      允许重试。

    ``sdk_loader`` 是测试注入点（提供 SDK 类与全局 setter/getter）；
    ``semantics``/``semantics_keys`` 由组合根注入 Application 层语义注册
    表与快照投影键（本模块不反向 import Application）。
    """
    global _RUNTIME, _PUBLISHED_ONCE
    if not settings.otel_enabled:
        return None
    config = _runtime_config(settings)
    with _STATE_LOCK:
        if _RUNTIME is not None:
            if _RUNTIME.config == config:
                _RUNTIME.refcount += 1
                return _RUNTIME.handle
            differing = sorted(
                key for key, value in config.items() if _RUNTIME.config.get(key) != value
            )
            return _diagnostic_handle(
                settings,
                "config_conflict",
                differing_fields=differing,
                semantics=semantics,
                semantics_keys=semantics_keys,
            )
        if _PUBLISHED_ONCE:
            return _diagnostic_handle(
                settings,
                "runtime_already_shutdown",
                semantics=semantics,
                semantics_keys=semantics_keys,
            )
        handle = _assemble(
            settings,
            sdk_loader if sdk_loader is not None else _default_sdk_loader(),
            semantics,
            semantics_keys,
        )
        if handle._published:
            handle._runtime = _SharedRuntime(config, handle)
            _RUNTIME = handle._runtime
            _PUBLISHED_ONCE = True
        # 未发布的失败句柄不缓存：允许重试。
        return handle


def get_runtime_handle() -> Optional[ObservabilityHandle]:
    """当前共享运行时句柄（无运行时/诊断句柄返回 None）。"""
    return _RUNTIME.handle if _RUNTIME is not None else None


def _drop_handler_attached() -> bool:
    return any(
        isinstance(h, _QueueFullDropHandler)
        for h in logging.getLogger(DROP_SOURCE_LOGGER).handlers
    )


def _reset_for_tests() -> None:
    global _RUNTIME, _PUBLISHED_ONCE
    with _STATE_LOCK:
        _RUNTIME = None
        _PUBLISHED_ONCE = False
    for handler in list(logging.getLogger(DROP_SOURCE_LOGGER).handlers):
        if isinstance(handler, _QueueFullDropHandler):
            logging.getLogger(DROP_SOURCE_LOGGER).removeHandler(handler)


def _diagnostic_handle(
    settings: Settings,
    error_code: str,
    *,
    differing_fields: Optional[List[str]] = None,
    semantics: Any = None,
    semantics_keys: Optional[Mapping[str, str]] = None,
) -> ObservabilityHandle:
    """独立 degraded 诊断句柄：不装配任何组件、不触碰共享运行时。"""
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    signals = {
        "traces": _SignalState(enabled=True),
        "metrics": _SignalState(enabled=True),
        "logs": _SignalState(enabled=True),
        "profile": _SignalState(enabled=settings.profiling_enabled),
    }
    for state in signals.values():
        state.degrade(error_code)
    if not settings.profiling_enabled:
        signals["profile"].status = _STATUS_DISABLED
        signals["profile"].delivery = _DELIVERY_DISABLED
    return ObservabilityHandle(
        mode=_resolve_mode(settings, endpoint),
        instance_id=uuid.uuid4().hex,
        sample_ratio=settings.otel_traces_sample_ratio,
        signals=signals,
        ring_buffer=_RingBufferSpanProcessor(semantics_keys),
        providers=[],
        local_metric_exporter=None,
        semantics=semantics,
        diagnostic_error=error_code,
        differing_fields=differing_fields,
    )


def _assemble(
    settings: Settings,
    loader: Any,
    semantics: Any = None,
    semantics_keys: Optional[Mapping[str, str]] = None,
) -> ObservabilityHandle:
    instance_id = uuid.uuid4().hex
    ratio = settings.otel_traces_sample_ratio

    signals = {
        "traces": _SignalState(enabled=True),
        "metrics": _SignalState(enabled=True),
        "logs": _SignalState(enabled=True),
        "profile": _SignalState(enabled=settings.profiling_enabled),
    }
    if not settings.profiling_enabled:
        signals["profile"].status = _STATUS_DISABLED
        signals["profile"].delivery = _DELIVERY_DISABLED
    else:
        # Profile worker 在 profiling.py（T6）装配；附加前 enabled 的
        # profile 信号报告 degraded。
        signals["profile"].degrade("profiler_not_configured")

    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    insecure: Optional[bool] = None
    if endpoint:
        from urllib.parse import urlparse

        insecure = urlparse(endpoint).scheme == "http"

    resource = loader.Resource(
        attributes={
            "service.name": SERVICE_NAME,
            "service.version": _service_version(),
            "service.instance.id": instance_id,
        }
    )
    owners: List[Any] = []  # 失败回滚时需要关闭的本模块资产
    exporters: List[_DeliveryStatusWrapper] = []
    publishes: List[tuple] = []  # (setter, provider)，仅在全部成功后应用
    ring_buffer = _RingBufferSpanProcessor(semantics_keys)
    metric_snapshot = MetricSnapshot(getattr(semantics, "INSTRUMENTS", {}),
                                     (semantics_keys or {}).get("metric_attribute_projector"))
    local_metric_exporter: Optional[_BoundedLocalMetricExporter] = None
    drop_handler: Optional[_QueueFullDropHandler] = None
    published = False

    try:
        # -- traces -----------------------------------------------------
        if loader.is_unclaimed(loader.get_tracer_provider()):
            if endpoint:
                trace_exporter = _DeliveryStatusWrapper(
                    loader.OTLPSpanExporter(
                        endpoint=endpoint,
                        insecure=insecure,
                        timeout=OTLP_TIMEOUT_SECONDS,
                    ),
                    signals["traces"],
                )
                exporters.append(trace_exporter)
                batch_processor = loader.BatchSpanProcessor(
                    trace_exporter,
                    max_queue_size=SPAN_QUEUE_MAX,
                    max_export_batch_size=EXPORT_BATCH_MAX,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
            else:
                # local-only：不创建网络 exporter，仅 ring buffer。
                batch_processor = None
                signals["traces"].delivery = _DELIVERY_DISABLED
            span_limits, log_limits = _detail_sdk_limits(settings.otel_content_max_bytes)
            tracer_provider = loader.TracerProvider(
                resource=resource, sampler=_build_sampler(ratio), span_limits=span_limits
            )
            owners.append(tracer_provider)
            tracer_provider.add_span_processor(ring_buffer)
            if batch_processor is not None:
                tracer_provider.add_span_processor(batch_processor)
            publishes.append((loader.set_tracer_provider, tracer_provider))
        else:
            # 外部拥有的全局 Provider：不覆盖、不关闭，仅本信号降级。
            signals["traces"].degrade("provider_unavailable")

        # -- metrics ----------------------------------------------------
        if loader.is_unclaimed(loader.get_meter_provider()):
            if endpoint:
                metric_exporter = _BoundedCardinalityMetricExporter(
                    _DeliveryStatusWrapper(
                        loader.OTLPMetricExporter(
                            endpoint=endpoint,
                            insecure=insecure,
                            timeout=OTLP_TIMEOUT_SECONDS,
                        ),
                        signals["metrics"],
                    )
                )
                metric_exporter._inner = _SnapshotMetricExporter(metric_exporter._inner, metric_snapshot)
                metric_reader = loader.PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=METRIC_EXPORT_PERIOD_SECONDS * 1000,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
            else:
                # local-only：有界聚合 reader，无网络。
                local_metric_exporter = _BoundedLocalMetricExporter()
                metric_exporter = _BoundedCardinalityMetricExporter(
                    local_metric_exporter
                )
                metric_exporter._inner = _SnapshotMetricExporter(metric_exporter._inner, metric_snapshot)
                metric_reader = loader.PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=METRIC_EXPORT_PERIOD_SECONDS * 1000,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
                signals["metrics"].delivery = _DELIVERY_DISABLED
            meter_provider = loader.MeterProvider(
                metric_readers=[metric_reader],
                resource=resource,
                # P062：保持默认 trace-based exemplar（显式传入，不关闭）。
                exemplar_filter=_trace_based_exemplar_filter(),
                views=_metric_views(semantics),
            )
            owners.append(meter_provider)
            publishes.append((loader.set_meter_provider, meter_provider))
        else:
            signals["metrics"].degrade("provider_unavailable")

        # -- logs -------------------------------------------------------
        if endpoint:
            if loader.is_unclaimed(loader.get_logger_provider()):
                log_exporter = _DeliveryStatusWrapper(
                    loader.OTLPLogExporter(
                        endpoint=endpoint,
                        insecure=insecure,
                        timeout=OTLP_TIMEOUT_SECONDS,
                    ),
                    signals["logs"],
                )
                exporters.append(log_exporter)
                log_processor = loader.BatchLogRecordProcessor(
                    log_exporter,
                    max_queue_size=LOG_QUEUE_MAX,
                    max_export_batch_size=EXPORT_BATCH_MAX,
                    export_timeout_millis=OTLP_TIMEOUT_SECONDS * 1000,
                )
                _, log_limits = _detail_sdk_limits(settings.otel_content_max_bytes)
                logger_provider = loader.LoggerProvider(resource=resource, log_record_limits=log_limits)
                owners.append(logger_provider)
                logger_provider.add_log_record_processor(log_processor)
                publishes.append((loader.set_logger_provider, logger_provider))
            else:
                signals["logs"].degrade("provider_unavailable")
        else:
            # local-only：无网络日志 exporter；应用日志留在自身 stderr 管道。
            signals["logs"].status = _STATUS_DISABLED
            signals["logs"].delivery = _DELIVERY_DISABLED

        # -- publish（仅在全部易失败构造成功后应用） ----------------------
        for setter, provider in publishes:
            setter(provider)
        published = True
    except Exception as exc:
        # 部分失败回滚：只关闭本模块构造的资产。全局 Provider 从未设置
        # （publish 最后执行），因此无需也无法回滚全局状态。
        for owner in owners:
            try:
                owner.shutdown()
            except Exception:
                pass
        for wrapper in exporters:
            try:
                wrapper.shutdown()
            except Exception:
                pass
        for state in signals.values():
            if state.enabled:
                state.degrade("setup_failed")
        try:
            # 安全 handler 尚未挂接，raw 异常文本可能经 lastResort 落 stderr
            # （常内嵌 endpoint URL）；只输出稳定错误码，不带原文。
            logging.getLogger("opentelemetry.nkb.setup").error(
                "observability setup failed code=setup_failed"
            )
        except Exception:
            pass
        return ObservabilityHandle(
            mode=_resolve_mode(settings, endpoint),
            instance_id=instance_id,
            sample_ratio=ratio,
            signals=signals,
            ring_buffer=ring_buffer,
            providers=[],
            local_metric_exporter=None,
            semantics=semantics,
            diagnostic_error="setup_failed",
        )

    handle = ObservabilityHandle(
        mode=_resolve_mode(settings, endpoint),
        instance_id=instance_id,
        sample_ratio=ratio,
        signals=signals,
        ring_buffer=ring_buffer,
        providers=owners,
        local_metric_exporter=local_metric_exporter,
        semantics=semantics,
    )
    handle._published = published
    handle._metric_snapshot = metric_snapshot
    if signals['metrics'].current_error:
        metric_snapshot.degrade(signals['metrics'].current_error)
    from .logging import SafeLogBridge
    otel_handler = None
    if endpoint and 'logger_provider' in locals() and getattr(loader, 'LoggingHandler', None):
        import warnings
        with warnings.catch_warnings():
            # SDK 1.45's supported compatibility bridge emits this migration
            # notice at construction. Keep the pinned SDK path until the
            # logging instrumentation dependency is adopted separately.
            warnings.filterwarnings("ignore", message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*", category=DeprecationWarning)
            otel_handler = loader.LoggingHandler(logger_provider=logger_provider)
    handle.log_bridge = SafeLogBridge(otel_handler, on_record=handle._log_buffer.append)
    if published:
        if 'meter_provider' in locals() and getattr(loader, "instrument_system_metrics", None) is not None:
            try:
                started = loader.instrument_system_metrics(meter_provider)
                signals["metrics"].extra["system_metrics"] = "started" if started else "failed"
            except Exception:
                signals["metrics"].extra["system_metrics"] = "failed"
        # T3 合同：启用时接通采集前安全投影（sanitizer + content 限额）。
        from .logging import prepare_sanitizer
        # Details and logging share one immutable-version secret catalog.
        configure_details(prepare_sanitizer().sanitize, settings.otel_content_max_bytes)
        # 队列满丢弃计数（仅存在 BSP/BLRP 的 export 模式需要）。
        if endpoint:
            handle._drop_handler = _QueueFullDropHandler(
                signals["traces"], signals["logs"]
            )
            logging.getLogger(DROP_SOURCE_LOGGER).addHandler(handle._drop_handler)
    return handle
