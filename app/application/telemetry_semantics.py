"""Operation lifecycle: stdlib and OTel API only; SDK assembly is injected."""

from __future__ import annotations
import asyncio
import contextvars
import logging
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
        _OperationState(state.name, state.span, merged, state.outcome)
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
                state.name, state.span, state.attributes, map_outcome(outcome)
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


def _measure_completion(spec, attrs, seconds):
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
        _measure_completion(spec, attrs, elapsed)
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
    def __init__(self, span, model):
        self._span = span
        self._model = model
        self._input_tokens = None

    def set_input_tokens(self, tokens):
        self._input_tokens = tokens if type(tokens) is int and tokens >= 0 else None

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


@contextmanager
def embedding_observer(model):
    model = map_model_name(model)
    with _operation_span(
        EMBEDDINGS_OPERATION,
        SpanKind.CLIENT,
        attrs=(
            (GEN_AI_OPERATION_NAME, EMBEDDINGS_OPERATION),
            (GEN_AI_REQUEST_MODEL, model),
            (GEN_AI_PROVIDER_NAME, "ollama"),
        ),
    ) as span:
        scope = EmbeddingObservationScope(span, model)
        try:
            yield scope
        finally:
            if scope._input_tokens is not None:
                _submit(**{GEN_AI_USAGE_INPUT_TOKENS: scope._input_tokens})


# Internal compatibility imports only; these templates are never emitted.
STAGE_COMPLETED_TEMPLATE = (
    "nkb.index.stage.completed stage=%s outcome=%s duration_ms=%.3f"
)
EMBEDDING_COMPLETED_TEMPLATE = "nkb.embedding.completed outcome=%s duration_ms=%.3f"
OPERATION_COMPLETED_TEMPLATE = (
    "nkb.operation.completed operation=%s outcome=%s duration_ms=%.3f"
)
