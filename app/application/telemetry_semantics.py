"""Operation lifecycle: stdlib and OTel API only; SDK assembly is injected."""

from __future__ import annotations
import asyncio
import contextvars
import logging
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional
from opentelemetry import metrics, trace
from opentelemetry.trace import INVALID_SPAN_CONTEXT, SpanKind, Status, StatusCode
from . import telemetry_contract as contract, telemetry_details
from .telemetry_contract import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_TOKEN_TYPE,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_PROVIDER_NAME,
    ERROR_TYPE_ATTR,
    INSTRUMENT_MODEL_COST,
    INSTRUMENT_MODEL_COST_COMPONENTS,
    INSTRUMENT_MODEL_COST_ATTEMPTS,
    NKB_MODEL_COST_STATUS,
    NKB_MODEL_COST_REASON,
    NKB_MODEL_COST_CURRENCY,
    NKB_MODEL_COST_DECIMAL,
    NKB_MODEL_COST_USD,
    NKB_MODEL_COST_INPUT_USD,
    NKB_MODEL_COST_COMPONENT,
    NKB_MODEL_PRICE_ID,
    NKB_MODEL_PRICING_VERSION,
    NKB_MODEL_BILLING_PROVIDER,
    NKB_MODEL_BILLING_MODEL,
    COST_BUCKETS,
    COST_REASONS,
    OUTCOMES,
    ERROR_TYPES,
    OBJECTS,
    INDEX_STAGES,
    OPERATIONS,
    IDENTITY_ATTRIBUTES,
    INSTRUMENTS,
    INSTRUMENT_OPERATION_DURATION,
    INSTRUMENT_TOKEN_USAGE,
    INSTRUMENT_INDEX_JOBS,
    INSTRUMENT_INDEX_CHUNKS,
    INSTRUMENT_RETRIEVAL_REQUESTS,
    INSTRUMENT_RETRIEVAL_RESULTS,
    INSTRUMENT_RETRIEVAL_DURATION,
    TOKEN_HISTOGRAM_BOUNDARIES,
    GENAI_DURATION_BOUNDARIES,
    NKB_DURATION_BOUNDARIES,
    CHUNK_HISTOGRAM_BOUNDARIES,
    RESULT_HISTOGRAM_BOUNDARIES,
    safe_id,
)
from .telemetry_payloads import embedding_input

SEMCONV_VERSION = "semantic-conventions-genai main @ 2026-10-24 (Development)"
DEFAULT_SERVICE = "n-kb"
_TRACER_NAME = "n-kb.application.telemetry_semantics"
_METER_NAME = _TRACER_NAME
EMBEDDINGS_OPERATION = "embeddings"
TOKEN_TYPE_INPUT = "input"
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = OUTCOME_ERROR = "failure"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_DENIED = "denied"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_SKIPPED = "skipped"
OUTCOME_DEFERRED = "deferred"
OUTCOME_UNKNOWN = OUTCOME_OTHER = "unknown"
ERROR_UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
ERROR_FILE_TOO_LARGE = "file_too_large"
ERROR_EMBEDDING_FAILED = "embedding_failed"
ERROR_VECTOR_INDEX_FAILED = "vector_index_failed"
ERROR_SQLITE_FAILED = "sqlite_failed"
ERROR_OTHER = "other"
NKB_RESULT_COUNT = "nkb.retrieval.result_count"
NKB_CHUNK_COUNT = "nkb.index.chunk_count"
NKB_DOCUMENT_ID = "nkb.index.document_id"
NKB_FILTER_PREFIX = "nkb.retrieval.filter_"
# Deprecated symbol only; no publisher writes this removed attribute.
NKB_OUTCOME = "nkb.outcome"
RETRIEVAL_SPAN_NAME = "retrieval.search"
INDEX_RUN_SPAN_NAME = "index.run"
INDEX_STAGE_SPAN_PREFIX = "index."
_STABLE_CODE_TO_ERROR_TYPE = MappingProxyType(
    {
        "embedding_provider_failed": "embedding_failed",
        "embedding_vector_mismatch": "embedding_failed",
        "qdrant_client_not_installed": "vector_index_failed",
    }
)


def map_error_type(value):
    return (
        value
        if type(value) is str and value in ERROR_TYPES
        else (
            _STABLE_CODE_TO_ERROR_TYPE.get(value, "other")
            if type(value) is str
            else "other"
        )
    )


def _is_timeout(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, TimeoutError):
            return True
        if any(
            cls.__module__ in ("httpx", "httpx._exceptions")
            and cls.__name__
            in (
                "TimeoutException",
                "ConnectTimeout",
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
            )
            for cls in type(exc).__mro__
        ):
            return True
        exc = exc.__cause__
    return False


def outcome_for_exception(exc):
    if exc is None:
        return OUTCOME_SUCCESS
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return OUTCOME_CANCELLED
    return OUTCOME_TIMED_OUT if _is_timeout(exc) else OUTCOME_FAILURE


def error_type_for_exception(exc):
    outcome = outcome_for_exception(exc)
    if exc is None or outcome == OUTCOME_CANCELLED:
        return None
    if outcome == OUTCOME_TIMED_OUT:
        return "timeout"
    if isinstance(exc, sqlite3.Error):
        return ERROR_SQLITE_FAILED
    try:
        code = str(exc)
    except Exception:
        return ERROR_OTHER
    return map_error_type(code)


def map_outcome(value):
    return value if type(value) is str and value in OUTCOMES else OUTCOME_UNKNOWN


def map_model_name(value):
    if type(value) is not str:
        return "other"
    value = value.strip()
    return value if contract.valid_attribute(GEN_AI_REQUEST_MODEL, value) else "other"


_safe_id = safe_id

# ---------------------------------------------------------------------------
# 成本事实词汇与安全投影（与 Domain cost 契约词汇镜像；本模块不 import 业务
# 模块，变更须同步 app/domain/cost.py）
# ---------------------------------------------------------------------------

# 受信计价身份的 provider 侧固定集合（计价目录 billing_provider 词汇 + other）。
# 计费身份是 env-only 受信配置，不是请求方事实。
_KNOWN_BILLING_PROVIDERS = frozenset({"ollama"})
# 八类不可估原因（与 Domain UnknownReason 词汇一致，源出 contract 登记表）
_KNOWN_COST_REASONS = COST_REASONS
# 成本分项桶到 Span/Log 金额键的映射（embeddings 只计 input 桶）
NKB_MODEL_COST_BUCKET_KEYS = MappingProxyType(
    {"input": NKB_MODEL_COST_INPUT_USD}
)
_BILLING_MODEL_MAX_LENGTH = 64


def map_billing_provider(provider):
    return (
        provider
        if type(provider) is str and provider in _KNOWN_BILLING_PROVIDERS
        else "other"
    )


def map_billing_model(model):
    """受信计费模型名的长度约束投影（不白名单化：目录 billing_model 是
    env-only 受信开放集合，白名单会把真实计费身份折叠成 other）。控制字符
    拒绝规则与 contract 的 billing_model 校验一致：Metric 维度不经过
    valid_attribute，必须在此对齐，避免控制字符进入 Counter 维度。"""
    if type(model) is not str:
        return "other"
    cleaned = model.strip()
    if not cleaned or len(cleaned) > _BILLING_MODEL_MAX_LENGTH:
        return "other"
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return "other"
    return cleaned


def _finite_nonneg_number(value) -> bool:
    """金额守卫：真实有限非负 int/float（bool 不是金额），显式 0 合法。"""
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and value >= 0
    )


def _cost_facts_from_result(result) -> dict:
    """计价结果 -> Span/Log 同键成本事实（不重算成本，只做安全投影）。

    estimated 携带全部金额/分项/decimal/版本/price_id/受信身份；unknown 只
    携带 status/reason（白名单内）/currency，且保留目录加载成功时的
    pricing_version（目录失效省略），绝不输出任何金额。非法或未知形态整组
    省略（延续旧防御行为），观测失败永不破坏业务。
    """
    try:
        if not isinstance(result, dict):
            return {}
        status = result.get("status")
        if status not in ("estimated", "unknown"):
            return {}
        facts = {NKB_MODEL_COST_STATUS: status}
        currency = result.get("currency")
        facts[NKB_MODEL_COST_CURRENCY] = (
            currency if type(currency) is str and currency.strip() else "USD"
        )
        billing_provider = result.get("billing_provider")
        if type(billing_provider) is str and billing_provider.strip():
            facts[NKB_MODEL_BILLING_PROVIDER] = map_billing_provider(billing_provider)
        billing_model = result.get("billing_model")
        if type(billing_model) is str and billing_model.strip():
            facts[NKB_MODEL_BILLING_MODEL] = map_billing_model(billing_model)
        if status == "unknown":
            reason = result.get("reason")
            if type(reason) is str and reason in _KNOWN_COST_REASONS:
                facts[NKB_MODEL_COST_REASON] = reason
            version = result.get("pricing_version")
            if type(version) is str and version.strip():
                facts[NKB_MODEL_PRICING_VERSION] = version
            # 唯一命中价格项后的 unknown 保留 price_id 供追溯；未定位到
            # 价格项的 unknown 该键缺省（组合根不携带即不投影）
            price_id = result.get("price_id")
            if type(price_id) is str and price_id.strip():
                facts[NKB_MODEL_PRICE_ID] = price_id
            return facts
        # estimated：核心三键缺一即整组省略（缺有效版本视为无效结果）
        amount = result.get("amount_usd")
        version = result.get("pricing_version")
        if not (type(version) is str and version.strip()):
            return {}
        if not _finite_nonneg_number(amount):
            return {}
        facts[NKB_MODEL_COST_USD] = float(amount)
        facts[NKB_MODEL_PRICING_VERSION] = version
        decimal_str = result.get("cost_decimal")
        if type(decimal_str) is str and decimal_str.strip():
            facts[NKB_MODEL_COST_DECIMAL] = decimal_str
        price_id = result.get("price_id")
        if type(price_id) is str and price_id.strip():
            facts[NKB_MODEL_PRICE_ID] = price_id
        components = result.get("components")
        if isinstance(components, dict):
            for bucket in COST_BUCKETS:
                bucket_key = NKB_MODEL_COST_BUCKET_KEYS.get(bucket)
                value = components.get(bucket)
                if bucket_key is not None and _finite_nonneg_number(value):
                    facts[bucket_key] = float(value)
        return facts
    except Exception:
        return {}


def _record_model_cost_metrics(result) -> None:
    """Record the three cost Counters for one finalized attempt.

    Consumes the RAW estimator result and re-validates internally: Metric
    recording is unconditional for any dict result with a known status, so a
    malformed estimated result (missing/blank pricing_version or a
    non-representable amount) still counts the attempt as ``unknown``
    (downgrade reason follows T1 order: pricing_unavailable before
    amount_unrepresentable) while no amount is ever emitted for it. Metric
    recording is independent of span sampling/recording state; the estimated
    branch always adds (explicit zero included) and the unknown branch only
    counts the attempt. Metric dimensions go through the bounded safe
    mappings; the metric model label goes through the ``map_model_name``
    whitelist (unknown -> ``other``) to keep counter cardinality bounded,
    while span/metadata billing identity keeps the raw billing projection
    (``map_billing_model``). Reason is whitelisted against the fixed
    eight-value vocabulary (estimated is pinned to ``none``).
    """
    try:
        if not isinstance(result, dict):
            return
        status = result.get("status")
        if status not in ("estimated", "unknown"):
            return
        amount = result.get("amount_usd")
        version = result.get("pricing_version")
        reason = result.get("reason")
        downgrade_reason = None
        if status == "estimated" and not (type(version) is str and version.strip()):
            status, downgrade_reason = "unknown", "pricing_unavailable"
        if status == "estimated" and not _finite_nonneg_number(amount):
            status, downgrade_reason = "unknown", "amount_unrepresentable"
        dims = {
            GEN_AI_OPERATION_NAME: EMBEDDINGS_OPERATION,
            NKB_MODEL_BILLING_PROVIDER: map_billing_provider(result.get("billing_provider")),
            NKB_MODEL_BILLING_MODEL: map_model_name(result.get("billing_model")),
        }
        attempt_dims = dict(dims)
        attempt_dims[NKB_MODEL_COST_STATUS] = status
        if status == "estimated":
            attempt_dims[NKB_MODEL_COST_REASON] = "none"
        elif downgrade_reason is not None:
            attempt_dims[NKB_MODEL_COST_REASON] = downgrade_reason
        elif type(reason) is str and reason in _KNOWN_COST_REASONS:
            attempt_dims[NKB_MODEL_COST_REASON] = reason
        _counter_add(INSTRUMENT_MODEL_COST_ATTEMPTS, 1, attempt_dims)
        if status != "estimated":
            return
        if _finite_nonneg_number(amount):
            _counter_add(INSTRUMENT_MODEL_COST, float(amount), dict(dims))
        components = result.get("components")
        if isinstance(components, dict):
            for bucket in COST_BUCKETS:
                value = components.get(bucket)
                if _finite_nonneg_number(value):
                    _counter_add(
                        INSTRUMENT_MODEL_COST_COMPONENTS,
                        float(value),
                        {**dims, NKB_MODEL_COST_COMPONENT: bucket},
                    )
    except Exception:
        pass


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
                name,
                unit=spec.unit,
                explicit_bucket_boundaries_advisory=spec.boundaries,
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


_operation_var = contextvars.ContextVar("nkb_operation_context", default=None)
_active_operation = contextvars.ContextVar("nkb_active_operation", default=None)
_completion_callback = None
_completion_scope_factory = None


@dataclass(frozen=True)
class CompletionSnapshot:
    """Sole completion publication. Callback must not add events or metrics.

    attributes is the identical immutable safe projection used by Span/Event/Log.
    Native IDs are absent for an invalid SpanContext. Details are already sanitized
    and bounded as flat nkb.content fields, and must not be re-expanded from extras.
    """

    event: str
    attributes: Mapping
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


@dataclass(frozen=True)
class _OperationState:
    name: str
    span: object
    attributes: Mapping
    outcome: Optional[str] = None
    # 原始计价结果（组合根闭包产出）；Metric 出口在收口时消费一次
    cost_result: Optional[Mapping] = None


def configure_completion(callback=None, scope_factory=None):
    """Inject one safe log publisher; None restores the marked stdlib log path."""
    if callback is not None and not callable(callback):
        raise ValueError("completion callback must be callable or None")
    if scope_factory is not None and not callable(scope_factory):
        raise ValueError("completion scope must be callable or None")
    global _completion_callback, _completion_scope_factory
    _completion_callback = callback
    _completion_scope_factory = scope_factory


@contextmanager
def _completion_scope(attributes):
    scope = None
    if _completion_scope_factory is not None:
        try:
            scope = _completion_scope_factory(attributes)
            scope.__enter__()
        except Exception:
            scope = None
    try:
        yield
    finally:
        if scope is not None:
            try:
                scope.__exit__(None, None, None)
            except Exception:
                pass


def current_operation_context():
    return _operation_var.get() or MappingProxyType({})


@contextmanager
def operation_context(**facts):
    parent = current_operation_context()
    inherited = {
        key: value for key, value in parent.items() if key in IDENTITY_ATTRIBUTES
    }
    merged = MappingProxyType(
        {**inherited, **contract.project_execution_attributes(facts)}
    )
    token = _operation_var.set(merged)
    try:
        yield merged
    finally:
        _operation_var.reset(token)


def _submit(**attributes):
    state = _active_operation.get()
    if state is None:
        return
    safe = contract.project_execution_attributes(attributes)
    merged = MappingProxyType({**state.attributes, **safe})
    _active_operation.set(
        _OperationState(state.name, state.span, merged, state.outcome, state.cost_result)
    )
    for key, value in safe.items():
        _set_attr(state.span, key, value)
    _operation_var.set(MappingProxyType({**current_operation_context(), **safe}))


def set_operation_result(outcome):
    """Report a factual terminal result; metrics/logs remain lifecycle-owned."""
    state = _active_operation.get()
    if state is not None:
        _active_operation.set(
            _OperationState(
                state.name, state.span, state.attributes, map_outcome(outcome),
                state.cost_result,
            )
        )


def set_span_attribute(span, key, value):
    if key in contract.CONTENT_ATTRIBUTES or not contract.valid_attribute(key, value):
        return
    state = _active_operation.get()
    if state is not None and state.span is span:
        _submit(**{key: value})
    else:
        _set_attr(span, key, value)


def set_index_chunk_count(span, chunk_count):
    set_span_attribute(span, NKB_CHUNK_COUNT, chunk_count)


def _publish_completion(snapshot):
    if _completion_callback is not None:
        _completion_callback(snapshot)
        return
    # The bridge must use the marked snapshot and never add another Event.
    level = (
        logging.WARNING
        if any(
            snapshot.attributes.get(f"nkb.{obj}.outcome") in ("failure", "timed_out")
            for obj in OBJECTS
        )
        else logging.INFO
    )
    logging.getLogger(__name__).log(
        level,
        snapshot.event,
        extra={"nkb_completion_snapshot": snapshot, "nkb_completion_owned": True},
    )


def _measure_completion(spec, attrs, seconds, cost_result=None):
    obj, action = spec.object, spec.action
    outcome = attrs[f"nkb.{obj}.outcome"]
    dims = {f"nkb.{obj}.outcome": outcome}
    if obj == "model":
        dimensions = {
            GEN_AI_OPERATION_NAME: EMBEDDINGS_OPERATION,
            GEN_AI_REQUEST_MODEL: attrs[GEN_AI_REQUEST_MODEL],
        }
        if ERROR_TYPE_ATTR in attrs:
            dimensions[ERROR_TYPE_ATTR] = attrs[ERROR_TYPE_ATTR]
        _histogram_record(INSTRUMENT_OPERATION_DURATION, seconds, dimensions)
        if GEN_AI_USAGE_INPUT_TOKENS in attrs:
            _histogram_record(
                INSTRUMENT_TOKEN_USAGE,
                attrs[GEN_AI_USAGE_INPUT_TOKENS],
                {
                    GEN_AI_OPERATION_NAME: EMBEDDINGS_OPERATION,
                    GEN_AI_REQUEST_MODEL: attrs[GEN_AI_REQUEST_MODEL],
                    GEN_AI_TOKEN_TYPE: TOKEN_TYPE_INPUT,
                },
            )
        # 三个成本 Counter：消费收口原始计价结果（内部再校验，畸形
        # estimated 降级 unknown 仍计 attempts）。Metric 记录不进
        # sampled/recording 判断；计价闭包缺失时无成本出口。
        if cost_result is not None:
            _record_model_cost_metrics(cost_result)
    elif obj == "index" and action in INDEX_STAGES:
        _histogram_record(
            "nkb.index.stage.duration", seconds, {**dims, "nkb.index.stage": action}
        )
    elif obj in ("document", "index", "retrieval", "mcp"):
        counter = {
            "document": "nkb.document.uploads",
            "index": "nkb.index.jobs",
            "retrieval": "nkb.retrieval.requests",
            "mcp": "nkb.mcp.requests",
        }[obj]
        duration = {
            "document": "nkb.document.upload.duration",
            "index": "nkb.index.duration",
            "retrieval": "nkb.retrieval.duration",
            "mcp": "nkb.mcp.duration",
        }[obj]
        if obj == "mcp":
            dims["nkb.mcp.operation"] = action
        _counter_add(counter, 1, dims)
        _histogram_record(duration, seconds, dims)
        if outcome == OUTCOME_SUCCESS:
            key = {"index": NKB_CHUNK_COUNT, "retrieval": NKB_RESULT_COUNT}.get(obj)
            if key is not None and key in attrs:
                _histogram_record(
                    (
                        INSTRUMENT_INDEX_CHUNKS
                        if obj == "index"
                        else INSTRUMENT_RETRIEVAL_RESULTS
                    ),
                    attrs[key],
                    {},
                )


def _finish_operation(spec, span, started, exc):
    state = _active_operation.get()
    outcome = (
        outcome_for_exception(exc)
        if exc is not None
        else state.outcome or OUTCOME_SUCCESS
    )
    # Only explicitly reported actual validation admission failures are denials.
    if (
        state.outcome == OUTCOME_DENIED
        and exc is not None
        and outcome == OUTCOME_FAILURE
    ):
        outcome = OUTCOME_DENIED
    elapsed = max(0.0, time.monotonic() - started)
    attrs = dict(state.attributes)
    attrs.update(
        {
            f"nkb.{spec.object}.outcome": outcome,
            f"nkb.{spec.object}.summary": spec.summary,
            f"nkb.{spec.object}.duration_ms": elapsed * 1000,
        }
    )
    if spec.object == "model":
        attrs["nkb.model.usage_known"] = GEN_AI_USAGE_INPUT_TOKENS in attrs
        # cost_status 派生事实：仅由已提交的成本事实推断，不凭空设置
        # （计价闭包缺失/失败时保持缺席）
        if NKB_MODEL_COST_STATUS not in attrs:
            if any(
                key in attrs
                for key in (NKB_MODEL_COST_USD,) + tuple(NKB_MODEL_COST_BUCKET_KEYS.values())
            ):
                attrs[NKB_MODEL_COST_STATUS] = "estimated"
            elif NKB_MODEL_COST_REASON in attrs:
                attrs[NKB_MODEL_COST_STATUS] = "unknown"
    if outcome in (OUTCOME_FAILURE, OUTCOME_TIMED_OUT):
        try:
            span.set_status(Status(StatusCode.ERROR))
        except Exception:
            pass
    if exc is not None:
        error = error_type_for_exception(exc)
        if outcome in (OUTCOME_FAILURE, OUTCOME_TIMED_OUT):
            if error is not None:
                attrs[ERROR_TYPE_ATTR] = error
        try:
            telemetry_details.update_current_details(
                error={"outcome": outcome, "type": error}
            )
        except Exception:
            attrs["nkb.observation.error"] = "details_unavailable"
    try:
        attrs.update(
            telemetry_details.flatten_details(telemetry_details.current_details())
        )
    except Exception:
        attrs["nkb.observation.error"] = "details_unavailable"
    attrs = MappingProxyType(contract.project_attributes(attrs))
    trace_id = span_id = None
    try:
        ctx = span.get_span_context()
        if ctx.is_valid:
            trace_id = f"{ctx.trace_id:032x}"
            span_id = f"{ctx.span_id:016x}"
    except Exception:
        pass
    snapshot = CompletionSnapshot(spec.event, attrs, trace_id, span_id)
    _operation_var.set(attrs)
    try:
        _measure_completion(
            spec, attrs, elapsed,
            state.cost_result if state is not None else None,
        )
    except Exception:
        pass
    with _completion_scope(snapshot.attributes):
        try:
            span.set_attributes(snapshot.attributes)
        except Exception:
            pass
        try:
            span.add_event(snapshot.event, attributes=snapshot.attributes)
        except Exception:
            pass
    try:
        _publish_completion(snapshot)
    except Exception:
        pass


@contextmanager
def _operation_span(name, kind=SpanKind.INTERNAL, attrs=(), facts=None):
    # Internal compatibility accepts legacy callsite names; exports are canonical.
    name = name.removeprefix("nkb.") if type(name) is str else ""
    spec = OPERATIONS.get(name)
    if spec is None:
        token = _active_operation.set(None)
        try:
            with (
                operation_context(**{"nkb.observation.error": "unknown_operation"}),
                telemetry_details.detail_scope(),
            ):
                try:
                    logging.getLogger(__name__).warning("unknown telemetry operation")
                except Exception:
                    pass
                yield _NoopSpan()
        finally:
            _active_operation.reset(token)
        return
    inherited = {
        key: value
        for key, value in current_operation_context().items()
        if key in IDENTITY_ATTRIBUTES
    }
    initial = contract.project_execution_attributes(
        {**inherited, **dict(attrs), **(facts or {})}
    )
    # Caller cannot prewrite result facts, summaries, durations or content.
    initial = {
        key: value
        for key, value in initial.items()
        if key in IDENTITY_ATTRIBUTES
        or key
        in (
            "nkb.index.stage",
            GEN_AI_OPERATION_NAME,
            GEN_AI_REQUEST_MODEL,
            GEN_AI_PROVIDER_NAME,
        )
    }
    if spec.object != "model":
        initial[f"nkb.{spec.object}.operation"] = spec.action
    started = time.monotonic()
    span = _start_span(
        (
            f'embeddings {initial.get(GEN_AI_REQUEST_MODEL,"other")}'
            if spec.object == "model"
            else name
        ),
        kind,
    )
    for key, value in initial.items():
        _set_attr(span, key, value)
    token = _active_operation.set(
        _OperationState(name, span, MappingProxyType(initial))
    )
    try:
        with (
            _span_scope(span),
            operation_context(**initial),
            telemetry_details.detail_scope(),
        ):
            exc = None
            try:
                yield span
            except BaseException as caught:
                exc = caught
                raise
            finally:
                try:
                    _finish_operation(spec, span, started, exc)
                except Exception:
                    pass
    finally:
        _active_operation.reset(token)
        _end_span(span)


operation_span = _operation_span
completed_operation = operation_span


def retrieval_span():
    return _operation_span(RETRIEVAL_SPAN_NAME)


def index_run_span(document_id=None):
    return _operation_span(
        INDEX_RUN_SPAN_NAME, attrs=((NKB_DOCUMENT_ID, safe_id(document_id)),)
    )


def index_stage_span_name(stage):
    return INDEX_STAGE_SPAN_PREFIX + stage if stage in INDEX_STAGES else "unknown"


def index_stage_span(stage):
    return _operation_span(
        index_stage_span_name(stage), facts={"nkb.index.stage": stage}
    )


def retrieval_attrs(result_count, filters=None):
    attrs = contract.project_attributes({NKB_RESULT_COUNT: result_count})
    if filters is not None:
        for field in ("tags", "source_kind", "document_status", "min_score"):
            value = (
                filters.get(field)
                if isinstance(filters, Mapping)
                else getattr(filters, field, None)
            )
            if field == "tags":
                if isinstance(value, Mapping):
                    attrs[NKB_FILTER_PREFIX + field] = len(value)
            elif value is not None:
                attrs[NKB_FILTER_PREFIX + field] = 1
    return attrs


def record_retrieval(outcome, *, result_count=None, seconds=None):
    """Compatibility fact submission; elapsed time is owned by the lifecycle."""
    state = _active_operation.get()
    if state is not None and state.name == RETRIEVAL_SPAN_NAME:
        set_operation_result(outcome)
        _submit(**{NKB_RESULT_COUNT: result_count})


def record_index(outcome, *, chunk_count=None):
    state = _active_operation.get()
    if state is not None and state.name == INDEX_RUN_SPAN_NAME:
        set_operation_result(outcome)
        _submit(**{NKB_CHUNK_COUNT: chunk_count})


class EmbeddingObservationScope:
    def __init__(self, span, model, raw_model=""):
        self._span = span
        self._model = model            # map 后的安全标签（观测展示用）
        self._raw_model = raw_model    # 原始模型名（计价精确身份承接用）
        self._input_tokens = None
        self._input_value = None
        self._input_state = "unknown"  # "known" | "invalid" | "unknown"

    def set_input_tokens(self, tokens):
        # 缺失（None）与非法（存在但非合法计数）分列承接，避免
        # invalid_usage 被折叠成 missing_usage；合法值建立 known。
        self._input_value = tokens
        if tokens is None:
            self._input_tokens = None
            self._input_state = "unknown"
        elif type(tokens) is int and tokens >= 0:
            self._input_tokens = tokens
            self._input_state = "known"
        else:
            self._input_tokens = None
            self._input_state = "invalid"

    def set_input(self, inputs):
        try:
            telemetry_details.update_current_details(
                embedding_input=embedding_input(self._model, inputs)
            )
        except Exception:
            pass

    def set_result(self, output_count, dimensions):
        telemetry_details.update_current_details(
            embedding_result={
                "model": self._model,
                "output_count": output_count,
                "dimensions": dimensions,
                "input_tokens": self._input_tokens,
            }
        )


# 组合根注入的 embeddings 计价闭包（默认 None：无计价出口，不产出成本事实）。
# 计价只消费受信身份与协议事实：闭包由组合根构造并绑定服务端受信配置
# （定价目录、billing_provider、billing_model），provider 适配器、请求
# options 与响应模型不能覆盖。
_cost_calculator = None


def configure_embedding_cost_calculator(calculator=None):
    """注入/清除 embeddings 计价闭包；非 callable 视为清除。"""
    global _cost_calculator
    _cost_calculator = calculator if callable(calculator) else None


def _finalize_embedding_cost(scope):
    """finalization 计价收口：向计价闭包提交协议事实，把原始结果挂到当前
    操作状态（收口时 Metric 出口消费一次），并把安全投影提交为 Span 与
    nkb.model.completed 事件事实。观测失败不破坏业务；计价失败同样只降级
    观测（吞异常，不改业务语义，也不产出部分成本事实）。"""
    try:
        calculator = _cost_calculator
        if not callable(calculator):
            return
        result = calculator(
            {
                "operation": EMBEDDINGS_OPERATION,
                "raw_model": scope._raw_model,
                "input_state": scope._input_state,
                "input_value": scope._input_value,
                "input_tokens": scope._input_tokens,
            }
        )
        if not isinstance(result, dict):
            return
        state = _active_operation.get()
        if state is not None and state.cost_result is None:
            _active_operation.set(
                _OperationState(
                    state.name, state.span, state.attributes, state.outcome, result
                )
            )
        facts = _cost_facts_from_result(result)
        if facts:
            _submit(**facts)
    except Exception:
        pass


@contextmanager
def embedding_observer(model):
    raw_model = model if type(model) is str else ""
    # 观测标签走白名单归一；原始模型名单独保留供计价精确查价
    model = map_model_name(raw_model)
    with _operation_span(
        EMBEDDINGS_OPERATION,
        SpanKind.CLIENT,
        attrs=(
            (GEN_AI_OPERATION_NAME, EMBEDDINGS_OPERATION),
            (GEN_AI_REQUEST_MODEL, model),
            (GEN_AI_PROVIDER_NAME, "ollama"),
        ),
    ) as span:
        scope = EmbeddingObservationScope(span, model, raw_model)
        try:
            yield scope
        finally:
            if scope._input_tokens is not None:
                _submit(**{GEN_AI_USAGE_INPUT_TOKENS: scope._input_tokens})
            _finalize_embedding_cost(scope)


# Internal compatibility imports only; these templates are never emitted.
STAGE_COMPLETED_TEMPLATE = (
    "nkb.index.stage.completed stage=%s outcome=%s duration_ms=%.3f"
)
EMBEDDING_COMPLETED_TEMPLATE = "nkb.embedding.completed outcome=%s duration_ms=%.3f"
OPERATION_COMPLETED_TEMPLATE = (
    "nkb.operation.completed operation=%s outcome=%s duration_ms=%.3f"
)
