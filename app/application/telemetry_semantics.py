"""N-KB telemetry semantics registry -- Application layer.

集中定义 N-KB 观测语义映射、属性白名单、instrument 名称与薄 helpers。
本模块只依赖标准库与 ``opentelemetry-api``；禁止 import SDK、exporter、
instrumentation、app.domain、app.config 或 app.infrastructure（架构边界测试
与 T7 组合根共同守护：Infrastructure 通过组合根注入拿到投影策略，不反向
import 本模块）。

Pinned semconv version: SEMCONV_VERSION =
"semantic-conventions-genai main @ 2026-10-24 (Development)"。GenAI 约定处于
Development 状态；草案破坏性变更时在本文件集中重映射，业务调用点不改动。

约定规则（对齐 spec-261029 Components 2）：
- GenAI 优先：embedding 调用只使用 GenAI embeddings 约定
  （``gen_ai.operation.name=embeddings``、``gen_ai.request.model``、
  ``gen_ai.usage.input_tokens``）；``nkb.*`` 仅补充 GenAI 未覆盖的 RAG 信号，
  同一信号只有一个约定名。
- 错误只输出安全 ``error.type`` 低基数枚举（unsupported_file_type /
  file_too_large / embedding_failed / vector_index_failed / sqlite_failed /
  other）。``embedding_provider_failed`` 与 ``embedding_vector_mismatch`` 映射
  ``embedding_failed``；其它已知业务稳定码映射对应枚举或 ``other``。原始异常
  message/stacktrace 永不进入 span 属性、事件或 metric 维度。cancelled 是
  outcome 而非错误：取消不扩充错误枚举（不写 ``error.type``，也不伪造
  ``CancelledError`` 类名）。
- 指标维度只允许有限枚举（operation、配置模型、stage、outcome、error.type）；
  document/job/trace/span ID、query、标签值、来源与原始 URL 不进入 metrics。
- 全部观测动作（span 创建、属性写入、metric 记录）失败即静默隔离，
  不改变业务返回值或异常传播路径。
"""
from __future__ import annotations

import asyncio
import logging
import contextvars
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping, Optional

from . import telemetry_details
from .telemetry_payloads import embedding_input

from opentelemetry import metrics, trace
from opentelemetry.trace import INVALID_SPAN_CONTEXT, SpanKind, Status, StatusCode

SEMCONV_VERSION = "semantic-conventions-genai main @ 2026-10-24 (Development)"

DEFAULT_SERVICE = "n-kb"

_TRACER_NAME = f"{DEFAULT_SERVICE}.application.telemetry_semantics"
_METER_NAME = _TRACER_NAME

_MAX_NAME_LENGTH = 64
_MAX_ID_LENGTH = 128
_ID_PATTERN = re.compile(r"[\w.:/-]+")
_MODEL_PATTERN = re.compile(r"[\w.:/@_-]+")

# ---------------------------------------------------------------------------
# GenAI attribute constants (pinned to the draft above)
# ---------------------------------------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"

ERROR_TYPE_ATTR = "error.type"

EMBEDDINGS_OPERATION = "embeddings"
TOKEN_TYPE_INPUT = "input"

# ---------------------------------------------------------------------------
# nkb.* supplement attributes (GenAI-uncovered RAG signals only)
# ---------------------------------------------------------------------------

NKB_RESULT_COUNT = "nkb.result_count"
NKB_CHUNK_COUNT = "nkb.chunk_count"
NKB_DOCUMENT_ID = "nkb.document_id"
NKB_OUTCOME = "nkb.outcome"
NKB_FILTER_PREFIX = "nkb.filter."

# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_OTHER = "other"
OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_ERROR, OUTCOME_CANCELLED, OUTCOME_OTHER})

# ---------------------------------------------------------------------------
# Safe error.type enum (strict spec set; cancelled is an outcome, not here)
# ---------------------------------------------------------------------------

ERROR_UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
ERROR_FILE_TOO_LARGE = "file_too_large"
ERROR_EMBEDDING_FAILED = "embedding_failed"
ERROR_VECTOR_INDEX_FAILED = "vector_index_failed"
ERROR_SQLITE_FAILED = "sqlite_failed"
ERROR_OTHER = "other"

ERROR_TYPES = frozenset(
    {
        ERROR_UNSUPPORTED_FILE_TYPE,
        ERROR_FILE_TOO_LARGE,
        ERROR_EMBEDDING_FAILED,
        ERROR_VECTOR_INDEX_FAILED,
        ERROR_SQLITE_FAILED,
        ERROR_OTHER,
    }
)

# Existing stable business codes -> spec error.type enum. Exact-match keys
# only (P058: no prefix/pattern matching, so raw exception text can never
# ride along); anything unknown collapses to ``other``.
_STABLE_CODE_TO_ERROR_TYPE: Mapping[str, str] = MappingProxyType(
    {
        "unsupported_file_type": ERROR_UNSUPPORTED_FILE_TYPE,
        "file_too_large": ERROR_FILE_TOO_LARGE,
        "embedding_provider_failed": ERROR_EMBEDDING_FAILED,
        "embedding_vector_mismatch": ERROR_EMBEDDING_FAILED,
        "qdrant_client_not_installed": ERROR_VECTOR_INDEX_FAILED,
        "document_not_found": ERROR_OTHER,
        "index_job_not_found": ERROR_OTHER,
        "invalid_tags": ERROR_OTHER,
        "invalid_storage_path": ERROR_OTHER,
        "infrastructure_error": ERROR_OTHER,
        "validation_error": ERROR_OTHER,
    }
)


def map_error_type(value: object) -> str:
    """Map a stable code / raw string through the bounded error-type enum."""
    if isinstance(value, str):
        if value in ERROR_TYPES:
            return value
        mapped = _STABLE_CODE_TO_ERROR_TYPE.get(value)
        if mapped is not None:
            return mapped
    return ERROR_OTHER


def outcome_for_exception(exc: Optional[BaseException]) -> str:
    """Business-terminal outcome for an escaped exception."""
    if exc is None:
        return OUTCOME_SUCCESS
    if isinstance(exc, asyncio.CancelledError):
        return OUTCOME_CANCELLED
    return OUTCOME_ERROR


def error_type_for_exception(exc: Optional[BaseException]) -> Optional[str]:
    """Safe ``error.type`` for an exception, or ``None`` when cancelled.

    ``str(exc)`` is used strictly as an exact-match lookup key against the
    known stable codes (this codebase raises ``ValueError("document_not_found")``
    style exceptions); the text itself is never emitted.
    """
    if exc is None:
        return None
    if isinstance(exc, asyncio.CancelledError):
        return None
    if isinstance(exc, sqlite3.Error):
        return ERROR_SQLITE_FAILED
    return map_error_type(str(exc))


def map_outcome(outcome: object) -> str:
    if isinstance(outcome, str) and outcome in OUTCOMES:
        return outcome
    return OUTCOME_OTHER


def map_model_name(model: object) -> str:
    """Bounded model mapping: models come only from configuration."""
    if not isinstance(model, str):
        return "other"
    cleaned = model.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        return "other"
    return cleaned if _MODEL_PATTERN.fullmatch(cleaned) else "other"


def _safe_id(value: object) -> Optional[str]:
    if isinstance(value, str) and 0 < len(value) <= _MAX_ID_LENGTH:
        return value if _ID_PATTERN.fullmatch(value) else None
    return None


# ---------------------------------------------------------------------------
# Instruments (spec metric table, item by item)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InstrumentSpec:
    kind: str  # "counter" | "histogram"
    unit: str
    boundaries: tuple = ()
    allowed_dimensions: tuple = ()


INSTRUMENT_OPERATION_DURATION = "gen_ai.client.operation.duration"  # Histogram/s
INSTRUMENT_TOKEN_USAGE = "gen_ai.client.token.usage"  # Histogram/{token}
INSTRUMENT_INDEX_JOBS = "nkb.index.jobs"  # Counter
INSTRUMENT_INDEX_CHUNKS = "nkb.index.chunks"  # Histogram
INSTRUMENT_RETRIEVAL_REQUESTS = "nkb.retrieval.requests"  # Counter
INSTRUMENT_RETRIEVAL_RESULTS = "nkb.retrieval.results"  # Histogram
INSTRUMENT_RETRIEVAL_DURATION = "nkb.retrieval.duration"  # Histogram/s

TOKEN_HISTOGRAM_BOUNDARIES = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144,
    1048576, 4194304, 16777216, 67108864,
)
GENAI_DURATION_BOUNDARIES = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 7.5, 10.0, 30.0,
)
NKB_DURATION_BOUNDARIES = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
CHUNK_HISTOGRAM_BOUNDARIES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)
RESULT_HISTOGRAM_BOUNDARIES = (0, 1, 2, 3, 5, 8, 10, 20, 50, 100)

INSTRUMENTS: Mapping[str, _InstrumentSpec] = MappingProxyType(
    {
        INSTRUMENT_OPERATION_DURATION: _InstrumentSpec(
            "histogram", "s", GENAI_DURATION_BOUNDARIES, (GEN_AI_OPERATION_NAME, GEN_AI_REQUEST_MODEL, ERROR_TYPE_ATTR)
        ),
        INSTRUMENT_TOKEN_USAGE: _InstrumentSpec(
            "histogram", "{token}", TOKEN_HISTOGRAM_BOUNDARIES, (GEN_AI_OPERATION_NAME, GEN_AI_REQUEST_MODEL, GEN_AI_TOKEN_TYPE)
        ),
        "nkb.document.uploads": _InstrumentSpec("counter", "1", allowed_dimensions=("outcome",)),
        "nkb.document.upload.duration": _InstrumentSpec("histogram", "s", NKB_DURATION_BOUNDARIES, ("outcome",)),
        "nkb.index.duration": _InstrumentSpec("histogram", "s", NKB_DURATION_BOUNDARIES, ("outcome",)),
        "nkb.index.stage.duration": _InstrumentSpec("histogram", "s", NKB_DURATION_BOUNDARIES, ("stage", "outcome")),
        "nkb.mcp.requests": _InstrumentSpec("counter", "1", allowed_dimensions=("operation", "outcome")),
        "nkb.mcp.duration": _InstrumentSpec("histogram", "s", NKB_DURATION_BOUNDARIES, ("operation", "outcome")),
        INSTRUMENT_INDEX_JOBS: _InstrumentSpec("counter", "1", allowed_dimensions=("outcome",)),
        INSTRUMENT_INDEX_CHUNKS: _InstrumentSpec(
            "histogram", "{chunk}", CHUNK_HISTOGRAM_BOUNDARIES
        ),
        INSTRUMENT_RETRIEVAL_REQUESTS: _InstrumentSpec("counter", "1", allowed_dimensions=("outcome",)),
        INSTRUMENT_RETRIEVAL_RESULTS: _InstrumentSpec(
            "histogram", "{result}", RESULT_HISTOGRAM_BOUNDARIES
        ),
        INSTRUMENT_RETRIEVAL_DURATION: _InstrumentSpec(
            "histogram", "s", NKB_DURATION_BOUNDARIES, ("outcome",)
        ),
    }
)


# ---------------------------------------------------------------------------
# Tracer / meter accessors and instrument handles
# ---------------------------------------------------------------------------


def _get_tracer():
    return trace.get_tracer(_TRACER_NAME)


def _get_meter():
    return metrics.get_meter(_METER_NAME)


_instrument_cache: dict = {}
_instrument_lock = threading.Lock()


def _get_instrument(name: str):
    with _instrument_lock:
        cached = _instrument_cache.get(name)
    if cached is not None:
        return cached
    spec = INSTRUMENTS[name]
    meter = _get_meter()
    if spec.kind == "counter":
        handle = meter.create_counter(name, unit=spec.unit)
    else:
        try:
            # Pinned opentelemetry-api 1.45.1 kwarg name.
            handle = meter.create_histogram(
                name, unit=spec.unit, explicit_bucket_boundaries_advisory=spec.boundaries
            )
        except TypeError:  # older OTel API without the advisory kwarg
            handle = meter.create_histogram(name, unit=spec.unit)
    with _instrument_lock:
        return _instrument_cache.setdefault(name, handle)


def _counter_add(name: str, value, dimensions: Mapping[str, str]) -> None:
    try:
        _get_instrument(name).add(value, dict(dimensions))
    except Exception:
        pass


def _histogram_record(name: str, value, dimensions: Mapping[str, str]) -> None:
    try:
        _get_instrument(name).record(value, dict(dimensions))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Span primitives (observability failure isolated; business exceptions
# including CancelledError propagate untouched; no exception events,
# no raw messages, no stacktraces)
# ---------------------------------------------------------------------------


class _NoopSpan(trace.NonRecordingSpan):
    """Non-recording fallback span (subclass of the API's own type)."""

    def __init__(self) -> None:
        super().__init__(INVALID_SPAN_CONTEXT)


def _start_span(name: str, kind: SpanKind):
    try:
        return _get_tracer().start_span(name, kind=kind)
    except Exception:
        return _NoopSpan()


def _set_attr(span, key: str, value) -> None:
    try:
        span.set_attribute(key, value)
    except Exception:
        pass


def _end_span(span) -> None:
    try:
        span.end()
    except Exception:
        pass


@contextmanager
def _span_scope(span):
    """Make ``span`` current without ending it; caller owns ``end()``.

    P057: ``tracer.start_span()`` + ``trace.use_span(end_on_exit=False)`` 显式
    组合，不用 current-span 装饰器封装。Auto exception recording/status are
    explicitly disabled; only safe error.type/status are written by us.
    """
    use = None
    try:
        use = trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )
        use.__enter__()
    except Exception:
        use = None
    try:
        yield
    finally:
        if use is not None:
            try:
                use.__exit__(None, None, None)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Operation context (ContextVar): 传播执行现场；进入子操作更新、退出恢复；
# 并发请求不串号。token 在 finally reset，覆盖嵌套、异常与取消。
# ---------------------------------------------------------------------------

_operation_var: contextvars.ContextVar[Optional[Mapping[str, str]]] = contextvars.ContextVar(
    "nkb_operation_context", default=None
)


def current_operation_context() -> Mapping[str, str]:
    context = _operation_var.get()
    return context if context is not None else MappingProxyType({})


@contextmanager
def operation_context(**facts) -> Iterator[Mapping[str, str]]:
    parent = _operation_var.get() or {}
    merged = MappingProxyType(
        {**parent, **{key: value for key, value in facts.items() if value is not None}}
    )
    token = _operation_var.set(merged)
    try:
        yield merged
    finally:
        try:
            _operation_var.reset(token)
        except ValueError:  # token belonged to a different context
            pass


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------

RETRIEVAL_SPAN_NAME = "nkb.retrieval.search"
INDEX_RUN_SPAN_NAME = "nkb.index.run"
INDEX_STAGE_SPAN_PREFIX = "nkb.index."
INDEX_STAGES = ("parse", "split", "embed", "write")


def index_stage_span_name(stage: object) -> str:
    if isinstance(stage, str) and stage in INDEX_STAGES:
        return INDEX_STAGE_SPAN_PREFIX + stage
    return INDEX_STAGE_SPAN_PREFIX + "other"


def _safe_stage(stage: object) -> str:
    if isinstance(stage, str) and stage in INDEX_STAGES:
        return stage
    return "other"


@contextmanager
def _operation_span(
    name: str,
    kind: SpanKind,
    attrs: tuple = (),
    facts: Optional[Mapping[str, str]] = None,
) -> Iterator[object]:
    """Operation span lifecycle shared by all nkb.* helpers.

    Span creation/attr/status/end failures are isolated; business exceptions
    and CancelledError propagate as-is. Outcome and safe error.type are
    written while the span is still current (P062: metrics recorded inside
    the same scope keep exemplar linkage available).
    """
    started = time.monotonic()
    span = _start_span(name, kind)
    for key, value in attrs:
        if value is not None:
            _set_attr(span, key, value)
    exc: Optional[BaseException] = None
    outcome = OUTCOME_SUCCESS
    try:
        with _span_scope(span), operation_context(**{"operation": name, **(facts or {})}), telemetry_details.detail_scope():
            try:
                yield span
            except BaseException as caught:
                exc = caught
                outcome = outcome_for_exception(caught)
                raise
            finally:
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    try:
                        span.set_status(Status(StatusCode.ERROR))
                    except Exception:
                        pass
                    _set_attr(span, ERROR_TYPE_ATTR, error_type_for_exception(exc))
                if exc is not None:
                    telemetry_details.update_current_details(error={"outcome":outcome,"type":error_type_for_exception(exc)})
                _set_attr(span, NKB_OUTCOME, outcome)
                _record_operation_terminal(name, outcome, time.monotonic() - started)
    finally:
        _end_span(span)


@contextmanager
def retrieval_span() -> Iterator[object]:
    """`nkb.retrieval.search` INTERNAL span（结果数、过滤条件类型与数量）。

    ``retrieval_attrs`` 提供投影属性；``record_retrieval`` 须在本 span 仍为
    current 时调用（jobs/requests 每业务终态一次）。
    """
    with _operation_span(RETRIEVAL_SPAN_NAME, SpanKind.INTERNAL) as span:
        yield span


@contextmanager
def index_run_span(document_id: Optional[str] = None) -> Iterator[object]:
    """`nkb.index.run` INTERNAL span（document_id、chunk 数、阶段结果）。"""
    attrs = ((NKB_DOCUMENT_ID, _safe_id(document_id) if document_id else None),)
    with _operation_span(
        INDEX_RUN_SPAN_NAME, SpanKind.INTERNAL, attrs=attrs
    ) as span:
        yield span


@contextmanager
def index_stage_span(stage: str) -> Iterator[object]:
    """`nkb.index.{parse|split|embed|write}` INTERNAL 阶段 span（含 outcome）。"""
    with _operation_span(
        index_stage_span_name(stage),
        SpanKind.INTERNAL,
        facts={"stage": _safe_stage(stage)},
    ) as span:
        yield span


def set_index_chunk_count(span, chunk_count) -> None:
    """Write the successful chunk count onto an index run span."""
    try:
        count = max(0, int(chunk_count))
    except Exception:
        return
    _set_attr(span, NKB_CHUNK_COUNT, count)


def safe_id(value: object) -> Optional[str]:
    """Bounded id projection（与操作 span 属性同一投影；无效返回 None）。"""
    return _safe_id(value)


def set_span_attribute(span, key: str, value) -> None:
    """安全写入单个 span 属性（失败静默隔离）。"""
    _set_attr(span, key, value)


@contextmanager
def operation_span(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attrs=(),
    facts: Optional[Mapping[str, str]] = None,
) -> Iterator[object]:
    """公共操作 span（本模块专用 helper 之外的 nkb.* 操作使用，如上传）。

    与全部 nkb.* 操作 span 共享同一生命周期：创建/属性/结束失败静默隔离；
    业务异常（含取消）原样传播；不记录异常事件，取消不伪造 error.type；
    outcome 与安全 error.type 在 span 仍 current 时写入。tracer 为本模块
    统一 tracer，operation_context 事实经 ``facts`` 发布。
    """
    with _operation_span(name, kind, attrs=tuple(attrs), facts=facts) as span:
        yield span


# ---------------------------------------------------------------------------
# retrieval_attrs：精确白名单投影（结果数 + 过滤条件类型与数量，不带值）
# ---------------------------------------------------------------------------

_FILTER_FIELDS = ("tags", "source_kind", "document_status", "min_score")


def retrieval_attrs(
    result_count: Optional[int],
    filters: object = None,
) -> dict:
    """Build the `nkb.retrieval.search` span attributes.

    只输出精确白名单键：``nkb.result_count`` 与 ``nkb.filter.<field>`` 计数。
    过滤条件的值（标签值、来源、分数）从不输出——只输出类型与数量
    （P058: 精确枚举键集合，无前缀放行）。``filters`` 支持属性访问对象或
    Mapping（调用方传入 domain ``RetrievalFilter`` 时无需本模块依赖它）。
    """
    attrs: dict = {}
    if result_count is not None:
        try:
            attrs[NKB_RESULT_COUNT] = max(0, int(result_count))
        except Exception:
            pass
    if filters is not None:
        for field in _FILTER_FIELDS:
            if isinstance(filters, Mapping):
                if field not in filters:
                    continue
                value = filters[field]
            else:
                if not hasattr(filters, field):
                    continue
                value = getattr(filters, field)
            if field == "tags":
                if isinstance(value, Mapping):
                    attrs[NKB_FILTER_PREFIX + field] = len(value)
            elif value is not None:
                attrs[NKB_FILTER_PREFIX + field] = 1
    return attrs


# ---------------------------------------------------------------------------
# Metric record helpers（业务终态一次；须在对应操作 span current 时调用）
# ---------------------------------------------------------------------------


def record_retrieval(
    outcome: str,
    *,
    result_count: Optional[int] = None,
    seconds: Optional[float] = None,
) -> None:
    """Record retrieval terminal-state metrics.

    ``nkb.retrieval.requests`` 每业务终态一次（outcome=success/error/cancelled）；
    ``nkb.retrieval.results`` 仅成功时记录实际结果数；``nkb.retrieval.duration``
    覆盖所有终态。调用方须在 ``nkb.retrieval.search`` span 仍为 current 时调用。
    """
    mapped = map_outcome(outcome)
    _counter_add(INSTRUMENT_RETRIEVAL_REQUESTS, 1, {"outcome": mapped})
    if mapped == OUTCOME_SUCCESS and result_count is not None:
        try:
            count = max(0, int(result_count))
        except Exception:
            count = None
        if count is not None:
            _histogram_record(INSTRUMENT_RETRIEVAL_RESULTS, count, {})
    if seconds is not None:
        try:
            elapsed = max(0.0, float(seconds))
        except Exception:
            elapsed = 0.0
        _histogram_record(INSTRUMENT_RETRIEVAL_DURATION, elapsed, {"outcome": mapped})


def record_index(outcome: str, *, chunk_count: Optional[int] = None) -> None:
    """Record index terminal-state metrics.

    ``nkb.index.jobs`` 每业务终态一次；``nkb.index.chunks`` 仅成功时记录
    实际 chunk 数。调用方须在 ``nkb.index.run`` span 仍为 current 时调用。
    """
    mapped = map_outcome(outcome)
    _counter_add(INSTRUMENT_INDEX_JOBS, 1, {"outcome": mapped})
    if mapped == OUTCOME_SUCCESS and chunk_count is not None:
        try:
            count = max(0, int(chunk_count))
        except Exception:
            count = 0
        _histogram_record(INSTRUMENT_INDEX_CHUNKS, count, {})


# ---------------------------------------------------------------------------
# Embedding operation scope factory（组合根注入 Infrastructure 的观测回调）
# ---------------------------------------------------------------------------


class EmbeddingObservationScope:
    """One real embedding model call: GenAI CLIENT span + duration/token metrics.

    Infrastructure（Ollama adapter，T7/T8 接入）通过组合根注入的工厂获得本
    scope：进入时创建 ``embeddings {model}`` CLIENT span，调用方以
    ``set_input_tokens`` 上报已知 token 数（缺失不伪造 0），退出时在 span
    仍为 current 时记录 duration 与 token usage；失败/取消同样记录终态与耗时。
    """

    def __init__(self, span, model: str) -> None:
        self._span = span
        self._model = model
        self._input_tokens: Optional[int] = None

    def set_input_tokens(self, tokens: Optional[int]) -> None:
        """Record the known input token count; ``None``/invalid = missing."""
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            self._input_tokens = None
            return
        self._input_tokens = tokens if tokens >= 0 else None

    def set_input(self, inputs):
        telemetry_details.update_current_details(embedding_input=embedding_input(self._model,inputs))

    def set_result(self, output_count, dimensions):
        telemetry_details.update_current_details(embedding_result={"model":self._model,"output_count":output_count,"dimensions":dimensions,"input_tokens":self._input_tokens})

    def _finish(self, seconds: float, exc: Optional[BaseException]) -> None:
        # Span is still current here (called from inside _span_scope) so the
        # histogram records carry exemplar linkage (P062).
        error_type = error_type_for_exception(exc)
        if self._input_tokens is not None:
            _set_attr(self._span, GEN_AI_USAGE_INPUT_TOKENS, self._input_tokens)
            _histogram_record(
                INSTRUMENT_TOKEN_USAGE,
                self._input_tokens,
                {
                    GEN_AI_OPERATION_NAME: EMBEDDINGS_OPERATION,
                    GEN_AI_REQUEST_MODEL: self._model,
                    GEN_AI_TOKEN_TYPE: TOKEN_TYPE_INPUT,
                },
            )
        dimensions = {
            GEN_AI_OPERATION_NAME: EMBEDDINGS_OPERATION,
            GEN_AI_REQUEST_MODEL: self._model,
        }
        if error_type is not None:
            dimensions[ERROR_TYPE_ATTR] = error_type
        _histogram_record(INSTRUMENT_OPERATION_DURATION, seconds, dimensions)


@contextmanager
def embedding_observer(model: object) -> Iterator[EmbeddingObservationScope]:
    """Scope factory for one embedding model call (GenAI embeddings operation).

    模型仅来自配置（经组合根传入）；失败/取消原样传播且同样记录终态与耗时；
    token 缺失不记 token usage，但 duration 覆盖所有终态。
    """
    mapped_model = map_model_name(model)
    span = _start_span(f"{EMBEDDINGS_OPERATION} {mapped_model}", SpanKind.CLIENT)
    _set_attr(span, GEN_AI_OPERATION_NAME, EMBEDDINGS_OPERATION)
    _set_attr(span, GEN_AI_REQUEST_MODEL, mapped_model)
    scope = EmbeddingObservationScope(span, mapped_model)
    start = time.monotonic()
    exc: Optional[BaseException] = None
    outcome = OUTCOME_SUCCESS
    try:
        with _span_scope(span), operation_context(
            operation=EMBEDDINGS_OPERATION, model=mapped_model
        ), telemetry_details.detail_scope():
            try:
                yield scope
            except BaseException as caught:
                exc = caught
                outcome = outcome_for_exception(caught)
                raise
            finally:
                try:
                    scope._finish(time.monotonic() - start, exc)
                except Exception:
                    pass
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    try:
                        span.set_status(Status(StatusCode.ERROR))
                    except Exception:
                        pass
                    _set_attr(span, ERROR_TYPE_ATTR, error_type_for_exception(exc))
                _set_attr(span, NKB_OUTCOME, outcome)
                if exc is not None:
                    telemetry_details.update_current_details(error={"outcome":outcome,"type":error_type_for_exception(exc)})
                try:
                    with operation_context(operation=EMBEDDINGS_OPERATION,outcome=outcome,duration_ms=(time.monotonic()-start)*1000):
                        logging.getLogger(__name__).log(logging.WARNING if outcome == OUTCOME_ERROR else logging.INFO,
                            EMBEDDING_COMPLETED_TEMPLATE,outcome,(time.monotonic()-start)*1000)
                except Exception:
                    pass
    finally:
        _end_span(span)


__all__ = [
    "SEMCONV_VERSION",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_TOKEN_TYPE",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "ERROR_TYPE_ATTR",
    "NKB_RESULT_COUNT",
    "NKB_CHUNK_COUNT",
    "NKB_DOCUMENT_ID",
    "NKB_OUTCOME",
    "NKB_FILTER_PREFIX",
    "OUTCOME_SUCCESS",
    "OUTCOME_ERROR",
    "OUTCOME_CANCELLED",
    "ERROR_TYPES",
    "ERROR_UNSUPPORTED_FILE_TYPE",
    "ERROR_FILE_TOO_LARGE",
    "ERROR_EMBEDDING_FAILED",
    "ERROR_VECTOR_INDEX_FAILED",
    "ERROR_SQLITE_FAILED",
    "ERROR_OTHER",
    "INSTRUMENT_OPERATION_DURATION",
    "INSTRUMENT_TOKEN_USAGE",
    "INSTRUMENT_INDEX_JOBS",
    "INSTRUMENT_INDEX_CHUNKS",
    "INSTRUMENT_RETRIEVAL_REQUESTS",
    "INSTRUMENT_RETRIEVAL_RESULTS",
    "INSTRUMENT_RETRIEVAL_DURATION",
    "RETRIEVAL_SPAN_NAME",
    "INDEX_RUN_SPAN_NAME",
    "INDEX_STAGES",
    "EmbeddingObservationScope",
    "map_error_type",
    "map_outcome",
    "map_model_name",
    "outcome_for_exception",
    "error_type_for_exception",
    "retrieval_span",
    "index_run_span",
    "index_stage_span",
    "index_stage_span_name",
    "set_index_chunk_count",
    "safe_id",
    "set_span_attribute",
    "operation_span",
    "retrieval_attrs",
    "record_retrieval",
    "record_index",
    "embedding_observer",
    "operation_context",
    "current_operation_context",
]


STAGE_COMPLETED_TEMPLATE = "nkb.index.stage.completed stage=%s outcome=%s duration_ms=%.3f"
EMBEDDING_COMPLETED_TEMPLATE = "nkb.embedding.completed outcome=%s duration_ms=%.3f"
OPERATION_COMPLETED_TEMPLATE = "nkb.operation.completed operation=%s outcome=%s duration_ms=%.3f"


def _record_operation_terminal(name, outcome, seconds):
    """Supplement missing operation metrics inside the owning span scope."""
    try:
        dims = {"outcome": map_outcome(outcome)}
        if name == "nkb.document.upload":
            _counter_add("nkb.document.uploads", 1, dims)
            _histogram_record("nkb.document.upload.duration", seconds, dims)
        elif name == INDEX_RUN_SPAN_NAME:
            _histogram_record("nkb.index.duration", seconds, dims)
        elif name.startswith(INDEX_STAGE_SPAN_PREFIX) and name.removeprefix(INDEX_STAGE_SPAN_PREFIX) in INDEX_STAGES:
            _histogram_record("nkb.index.stage.duration", seconds, {**dims, "stage": name.removeprefix(INDEX_STAGE_SPAN_PREFIX)})
        elif name == "nkb.mcp.status":
            dims["operation"] = name
            _counter_add("nkb.mcp.requests", 1, dims)
            _histogram_record("nkb.mcp.duration", seconds, dims)
        if name.startswith(INDEX_STAGE_SPAN_PREFIX) and name.removeprefix(INDEX_STAGE_SPAN_PREFIX) in INDEX_STAGES:
            with operation_context(operation=name,outcome=outcome,duration_ms=seconds*1000):
                logging.getLogger(__name__).log(logging.WARNING if outcome == OUTCOME_ERROR else logging.INFO,
                    STAGE_COMPLETED_TEMPLATE,name.removeprefix(INDEX_STAGE_SPAN_PREFIX),outcome,seconds*1000)
        if name in ("nkb.document.upload", "nkb.mcp.status"):
            with operation_context(operation=name, outcome=outcome, duration_ms=seconds * 1000):
                logging.getLogger(__name__).log(logging.WARNING if outcome == OUTCOME_ERROR else logging.INFO,
                                               OPERATION_COMPLETED_TEMPLATE, name, outcome, seconds * 1000)
    except Exception:
        pass


completed_operation = operation_span
