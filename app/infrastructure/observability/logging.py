"""Safe, bounded logging outlets and exact registered terminal events.

One Prepared projection supplies readable sanitized stderr, Span attributes /
events and OTLP logs. Unknown application/library logs stay readable once the
startup sanitizer is ready. Untrusted extras always enter log_fields through
the injected Application policy; record attributes never establish identity or
prepared provenance. Self-inspection receives facts only. Owned handlers keep
stdout free for MCP and leave foreign handlers untouched.

Only stdlib, OTel API and this infrastructure package are imported here.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
import time
import weakref
from itertools import islice
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from opentelemetry import trace as trace_api

from .sanitizer import LogSanitizer  # noqa: F401  (re-export)
from .snapshots import project_log_attributes
from . import sanitizer as detail_policy

# Registry lookup fallback; project_record classifies readable unknown logs.
UNKNOWN_EVENT_CODE = "diagnostic_log"
DIAGNOSTIC_EVENT_CODE = "diagnostic_log"
HTTP_COMPLETED_EVENT = "nkb.http.completed"
HTTP_CLIENT_COMPLETED_EVENT = "nkb.http.completed"
_LIFECYCLE_EVENTS = frozenset(f"nkb.{obj}.completed" for obj in
    ("document", "index", "retrieval", "model", "mcp", "http"))

# 观测自激防护：这些 logger 的记录绝不进入 OTel 桥。
_SELF_TRAFFIC_PREFIXES = ("opentelemetry", "grpc")

# 访问事实事件为模块内置注册（组合根无需重复登记）。
_BUILTIN_EVENTS = (
    (HTTP_COMPLETED_EVENT + " %s", HTTP_COMPLETED_EVENT),
    (HTTP_CLIENT_COMPLETED_EVENT + " %s", HTTP_CLIENT_COMPLETED_EVENT),
)

# exception.type 只接受受限类型名模式（点分 Python 标识符），防注入。
_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")

# 访问事实结果的固定枚举。
_OUTCOMES = frozenset(("success", "failure", "denied", "cancelled", "timed_out", "skipped", "deferred", "unknown"))

_OBS_LOGGER_NAME = __name__

_registry_lock = threading.Lock()
_REGISTRY: dict = {}
_CODE_VALUES: set = set()

_sanitizer: LogSanitizer | None = None
_boundary_warned = False
_OWNED_ATTR = "_nkb_observability_owned"
_execution_context_snapshot = None
_execution_project = None
_completion_records = weakref.WeakKeyDictionary()
_published_records = weakref.WeakSet()
_detail_callbacks = {}
_content_max_bytes = 1024000
_projection_generation = 0
_prepared_records = weakref.WeakKeyDictionary()
_owned_records = weakref.WeakKeyDictionary()
_bridge_active = ContextVar("nkb_log_bridge_active", default=False)
_prepared_outlet = ContextVar("nkb_prepared_outlet", default=None)
_projection_active = ContextVar("nkb_projection_active", default=False)
_json_active = ContextVar("nkb_json_active", default=False)
_diagnostic_active = ContextVar("nkb_diagnostic_active", default=False)
_last_diagnostic_at = 0.0


def configure_execution_context(*, snapshot=None, project=None, validate=None,
                                flatten=None, details_snapshot=None, budget=None,
                                max_bytes=1024000, execution_project=None):
    """Composition injects Application context without reversing dependency."""
    global _execution_context_snapshot, _execution_project, _detail_callbacks, _content_max_bytes, _projection_generation
    _execution_context_snapshot = snapshot
    _execution_project = execution_project
    from .snapshots import configure_execution_projector
    configure_execution_projector(execution_project)
    _completion_records.clear()
    _detail_callbacks = dict(project=project, validate=validate, flatten=flatten,
                            details_snapshot=details_snapshot, budget=budget)
    _content_max_bytes = max(1, min(1024000, max_bytes))
    _projection_generation += 1
    _prepared_records.clear()
    _owned_records.clear()


# ---------------------------------------------------------------------------
# 内部注册表维护
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    for template, code in _BUILTIN_EVENTS:
        _REGISTRY[template] = code
        _CODE_VALUES.add(code)


_register_builtins()


def _reset_registry() -> None:
    _REGISTRY.clear()
    _CODE_VALUES.clear()
    _register_builtins()


# ---------------------------------------------------------------------------
# sanitizer 装配（组合根调用；未装配时内容投影 fail closed）
# ---------------------------------------------------------------------------


def prepare_sanitizer() -> LogSanitizer:
    """创建并发布进程级内容 sanitizer（幂等）；返回当前实例。"""
    global _sanitizer
    with _registry_lock:
        if _sanitizer is None:
            _sanitizer = LogSanitizer()
        return _sanitizer


def ensure_sanitizer() -> LogSanitizer | None:
    """当前 sanitizer（未装配返回 None）。"""
    return _sanitizer


def configure_secret_catalog(known_secrets) -> None:
    """登记已知秘密值目录（如 provider API key），先于规则遮盖。"""
    sanitizer = _sanitizer
    if sanitizer is not None:
        sanitizer.configure_secret_catalog(known_secrets)


def _sanitize_content(text):
    """正文脱敏；sanitizer 未装配或失败时返回 None（fail closed）。"""
    if type(text) is not str:
        return None
    sanitizer = _sanitizer
    if sanitizer is None:
        return None
    try:
        result = sanitizer.sanitize(text)
    except Exception:
        return None
    return result if type(result) is str else None


def sanitize_text(text: str) -> str:
    """结构性安全文本（路由模板、事件摘要）脱敏。

    模板仅由白名单段规则构成、原始 URL 内容不会进入；sanitizer 未装配时
    原样返回（后续段规则与限长仍生效），失败时返回空串。
    """
    if type(text) is not str:
        return ""
    sanitizer = _sanitizer
    if sanitizer is None:
        return text
    try:
        result = sanitizer.sanitize(text)
    except Exception:
        return ""
    return result if type(result) is str else ""


# ---------------------------------------------------------------------------
# 事件注册表（精确模板枚举，P058）
# ---------------------------------------------------------------------------


def register_safe_event(template: str, code: str) -> None:
    """登记 ``消息模板 -> 事件码``；同模板重复登记必须同码（幂等）。"""
    if type(template) is not str or not template:
        raise ValueError("event template must be a non-empty string")
    if type(code) is not str or not code:
        raise ValueError("event code must be a non-empty string")
    with _registry_lock:
        existing = _REGISTRY.get(template)
        if existing is not None and existing != code:
            raise ValueError(f"event template already registered: {template}")
        _REGISTRY[template] = code
        _CODE_VALUES.add(code)


def event_code_for_template(template):
    """完整模板精确匹配；未注册返回 None（绝不前缀/子串放行）。"""
    if type(template) is not str:
        return None
    return _REGISTRY.get(template)


def event_code_for(record) -> str:
    """LogRecord 的事件码：完整模板命中或 ``unknown``。"""
    if record is None:
        return UNKNOWN_EVENT_CODE
    template = getattr(record, "msg", None)
    code = event_code_for_template(template) if type(template) is str else None
    return code if code is not None else UNKNOWN_EVENT_CODE


def is_registered_event(event):
    return type(event) is str and event in _CODE_VALUES


def project_flat_details(attributes):
    """Rebuild only the 11 exact envelopes, and freshly validate content."""
    if _prepared_outlet.get() is attributes:
        return {key:value for key,value in attributes.items() if key in _DETAIL_ATTRIBUTE_KEYS}
    if not isinstance(attributes, dict) and not hasattr(attributes, "items"):
        return {}
    envelopes = {}
    for name in detail_policy.FIELDS:
        prefix = "nkb.content." + name
        content = attributes.get(prefix)
        if type(content) is not str:
            continue
        envelopes[name] = dict(content=content,
            original_bytes=attributes.get(prefix + "_original_bytes"),
            truncated=attributes.get(prefix + "_truncated", False))
        for key in ("incomplete", "observed_bytes"):
            if prefix + "_" + key in attributes:
                envelopes[name][key] = attributes[prefix + "_" + key]
    safe = _call("validate", envelopes)
    _, _, safe = _fit_content(None, None, safe, {})
    return _call("flatten", safe)


def is_prepared_outlet(attributes):
    return _prepared_outlet.get() is attributes


def content_max_bytes():
    return _content_max_bytes


def details_capture_enabled():
    return callable(_detail_callbacks.get('project'))


def detail_policy_token():
    return (_projection_generation, id(_sanitizer), getattr(_sanitizer,"policy_version",None))


def fit_safe_flat_details(flat):
    """Fit only already projected detail buffers; never retain raw inputs."""
    envelopes = {}
    for name in detail_policy.FIELDS:
        prefix = "nkb.content."+name
        if prefix not in flat:
            continue
        envelopes[name] = {key:flat[(prefix if key == "content" else prefix+"_"+key)] for key in
            ("content","original_bytes","truncated","incomplete","observed_bytes") if (prefix if key == "content" else prefix+"_"+key) in flat}
        envelopes[name].setdefault("original_bytes",None)
    _, _, safe = _fit_content(None,None,envelopes,{})
    return _call("flatten",safe)


def project_event_attributes(attributes):
    if _prepared_outlet.get() is attributes:
        return dict(attributes)
    attributes = dict(attributes or {})
    safe = project_log_attributes(attributes)
    flat = project_flat_details(attributes)
    envelopes = {}
    for name in detail_policy.FIELDS:
        prefix = "nkb.content." + name
        if prefix in flat:
            envelopes[name] = {key:flat[(prefix if key == "content" else prefix+"_"+key)] for key in
                ("content","original_bytes","truncated","incomplete","observed_bytes") if (prefix if key == "content" else prefix+"_"+key) in flat}
            envelopes[name].setdefault("original_bytes", None)
    text = {}
    for key in ("message", "exception.type", "exception.message", "exception.stacktrace"):
        value = attributes.get(key)
        if type(value) is str:
            text[key] = _clip(_safe_value(value), _content_max_bytes)
    exception = {key:text["exception."+key] for key in ("type","message","stacktrace") if "exception."+key in text}
    message, exception, envelopes = _fit_content(text.get("message"), exception or None, envelopes, safe)
    safe.update(_call("flatten", envelopes))
    if message:
        safe["message"] = message
    if exception:
        safe.update({"exception."+key:value for key,value in exception.items()})
    return safe


_DETAIL_ATTRIBUTE_KEYS = frozenset(
    "nkb.content."+name+suffix for name in detail_policy.FIELDS
    for suffix in ("", "_original_bytes", "_truncated", "_incomplete", "_observed_bytes"))



# ---------------------------------------------------------------------------
# 采集前投影
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """One bounded, sanitized presentation shared by every owned outlet."""
    event: str
    message: str | None
    exception_type: str | None
    details: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    exception: dict | None = None
    attributes: dict = field(default_factory=dict)
    owner: object = field(default=None, repr=False, compare=False)
    trace_id: str | None = None
    span_id: str | None = None
    dropped: bool = False
    logger_name: str = "library"


def _exception_type_of(exc_info):
    exc = exc_info[1] if type(exc_info) is tuple and len(exc_info) > 1 else exc_info
    name = type(exc).__name__ if isinstance(exc, BaseException) else None
    if type(name) is not str or not _EXCEPTION_TYPE.fullmatch(name):
        return None
    if _sanitizer is None:
        return name
    safe = _sanitize_content(name)
    return safe if type(safe) is str and _EXCEPTION_TYPE.fullmatch(safe) else "Exception"


def _clip(text, limit):
    return text[:limit].encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def _bounded_value(value, depth=0, budget=None):
    """Formatting never invokes user-defined __str__, __repr__ or iterators."""
    if budget is None:
        budget = [128, _content_max_bytes * 4]
    budget[0] -= 1
    if budget[0] < 0:
        return "[format truncated]"
    kind = type(value)
    if kind is str:
        size = min(len(value), budget[1])
        prefix = value[:size]
        encoded = prefix.encode("utf-8", errors="replace")
        complete = len(value) <= size and len(encoded) <= budget[1]
        budget[1] = max(0, budget[1] - len(encoded))
        if not complete:
            return "[DETAILS INCOMPLETE]"
        return _sanitize_content(prefix) or "[content unavailable]"
    if kind in (int, float, bool, type(None)):
        return value if kind is not int or value.bit_length() < 1024 else "[large integer]"
    if depth < 4 and kind in (list, tuple):
        return tuple(_bounded_value(v, depth + 1, budget) for v in value[:32] if budget[0] > 0)
    if depth < 4 and kind is dict:
        return {k:_bounded_value(v, depth + 1, budget) for k,v in islice(value.items(),32)
                if type(k) is str and len(k) <= 128 and budget[0] > 0}
    return "[" + type(value).__name__[:64] + "]"


def _safe_value(value):
    bounded = _bounded_value(value)
    if type(bounded) is dict or (type(bounded) is tuple and any(type(item) in (dict,tuple,list) for item in bounded)):
        # Python repr uses single quotes and cannot enforce structured business
        # boundaries. Reuse the sole injected policy, then recover bounded JSON
        # primitives so numeric printf conversions keep their normal behavior.
        projected = _call("project", log_fields={"format_arguments":bounded})
        content = projected.get("log_fields",{}).get("content")
        try:
            cleaned = json.loads(content)["format_arguments"] if type(content) is str else None
        except (ValueError,KeyError,TypeError):
            cleaned = None
        if type(bounded) is dict:
            return cleaned if type(cleaned) is dict else "[dict unavailable]"
        return tuple(cleaned) if type(cleaned) is list else ("[container unavailable]",)
    return bounded


def _safe_message(record):
    if record.msg is None or (type(record.msg) in (dict,list,tuple) and not record.msg):
        return ""
    if type(record.msg) is not str:
        return "[" + type(record.msg).__name__[:64] + " log]"
    if len(record.msg) > _content_max_bytes * 4:
        return "[DETAILS INCOMPLETE]"
    template = record.msg
    # Reject width/precision bombs before the builtin interpolation engine.
    if re.search(r"%[^%a-zA-Z]*(?:[0-9]{4,}|\*)", template):
        return "[log format unavailable]"
    try:
        args = _safe_value(record.args)
        return template % args if args else template
    except Exception:
        return template + " [log arguments unavailable]"


def _safe_exception(exc_info):
    exc = exc_info[1] if type(exc_info) is tuple and len(exc_info) > 1 else exc_info
    if not isinstance(exc, BaseException) or _sanitizer is None:
        return None
    seen, chain, frames = set(), [], []
    first_message = None
    for _ in range(4):
        if exc is None or id(exc) in seen:
            break
        seen.add(id(exc))
        # BaseException's own descriptors avoid hostile subclass properties.
        args = BaseException.args.__get__(exc)
        message = "; ".join(str(_safe_value(v)) for v in args[:8])
        safe = _sanitize_content(message)
        if safe is None:
            return None
        if first_message is None:
            first_message = _clip(safe, _content_max_bytes)
        chain.append(type(exc).__name__[:128] + ": " + _clip(safe, _content_max_bytes))
        tb = BaseException.__traceback__.__get__(exc)
        for _ in range(16 - len(frames)):
            if tb is None:
                break
            # No locals, source lines or absolute file paths.
            frames.append("  at " + tb.tb_frame.f_code.co_name[:128] + ":" + str(tb.tb_lineno))
            tb = tb.tb_next
        cause = BaseException.__cause__.__get__(exc)
        context = BaseException.__context__.__get__(exc)
        exc = cause if cause is not None else (None if BaseException.__suppress_context__.__get__(exc) else context)
    stack = _sanitize_content("\n".join(chain + frames))
    return dict(type=_exception_type_of(exc_info), message=first_message or "",
                stacktrace=_clip(stack or "", _content_max_bytes))


def _is_library_noise(record):
    return (type(record.name) is str and
            any(record.name == p or record.name.startswith(p + ".") for p in ("httpx", "httpcore"))
            and record.levelno <= logging.INFO and not record.exc_info)


class _Unavailable(dict):
    details_unavailable = True


def _safe_diagnostic():
    """One fixed stderr diagnostic per second; no record/exception formatting."""
    global _last_diagnostic_at
    if _diagnostic_active.get():
        return
    now = time.monotonic()
    with _registry_lock:
        if now - _last_diagnostic_at < 1:
            return
        _last_diagnostic_at = now
    token = _diagnostic_active.set(True)
    try:
        sys.stderr.write("n-kb observability: content projection unavailable\n")
    except Exception:
        pass
    finally:
        _diagnostic_active.reset(token)


def _call(name, *args, **kwargs):
    callback = _detail_callbacks.get(name)
    if callback is None:
        return _Unavailable() if name == "project" and kwargs else {}
    try:
        return callback(*args, **kwargs)
    except Exception:
        _safe_diagnostic()
        return _Unavailable()


def _body_size(message, exception, details):
    nested = dict(message=message, exception=exception, details=details)
    flat = {("nkb.content."+name if key == "content" else "nkb.content."+name+"_"+key):value
            for name,item in details.items() for key,value in item.items() if value is not None}
    if message:
        flat["message"] = message
    if exception:
        flat.update({"exception."+key:value for key,value in exception.items() if value is not None})
    # OTel log body and attributes both carry the prepared message. Count both,
    # including repeated flattened key names and serialization escapes.
    outlets = (nested, dict(body=message, attributes=flat))
    return max(len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode("utf-8"))
               for value in outlets)


def _fit_content(message, exception, details, facts):
    total = _content_max_bytes * 4
    # Reserve the exact JSON envelope overhead before details allocation.
    # Fixed facts/identity are intentionally excluded from this content budget.
    if total < _body_size(None, None, {}):
        message, exception, details = None, None, {}
        facts["nkb.observation.error"] = "details_unavailable"
    else:
        while _body_size(message, exception, {}) > total:
            if exception and exception.get("stacktrace"):
                exception["stacktrace"] = _clip(exception["stacktrace"], max(0,len(exception["stacktrace"].encode()) // 2))
            elif exception:
                exception = None
            elif message:
                message = _clip(message, max(0,len(message.encode()) // 2))
            else:
                break
        remaining = total - _body_size(message, exception, {}) + 2
        details = _call("budget", details, remaining) if details else {}
        for _ in range(12):
            excess = _body_size(message, exception, details) - total
            if excess <= 0:
                break
            remaining = max(0, remaining - excess)
            details = _call("budget", details, remaining)
        if _body_size(message, exception, details) > total:
            details = {}
            facts["nkb.observation.error"] = "details_unavailable"
    if getattr(details,"details_unavailable",False):
        facts["nkb.observation.error"] = "details_unavailable"
    return message, exception, details


def project_record(record):
    if _projection_active.get():
        _safe_diagnostic()
        return Prepared(DIAGNOSTIC_EVENT_CODE, None, None, dropped=True)
    token = _projection_active.set(True)
    try:
        return _prepare_record(record)
    finally:
        _projection_active.reset(token)


def _prepare_record(record):
    if record is None:
        return Prepared(UNKNOWN_EVENT_CODE, None, None, dropped=True)
    version = (_projection_generation, id(_sanitizer), getattr(_sanitizer, "policy_version", None))
    cached = _prepared_records.get(record)
    if cached is not None and cached[0] == version:
        return cached[1]
    completion = _completion_records.get(record)
    if completion is not None:
        attrs = dict(completion.attributes)
        facts = project_log_attributes(attrs)
        details = _envelopes_from_flat(attrs)
        message = next((value for key,value in attrs.items() if key.endswith(".summary")), completion.event)
        prepared = Prepared(completion.event, message, None, details, facts,
                            None, attrs, trace_api.get_current_span(), completion.trace_id,
                            completion.span_id, False, record.name)
        _prepared_records[record] = (version, prepared)
        return prepared
    stable = event_code_for_template(record.msg) if type(record.msg) is str else None
    if stable in _LIFECYCLE_EVENTS and record not in _owned_records:
        stable = None
    event = stable or (DIAGNOSTIC_EVENT_CODE if record.levelno >= logging.WARNING else
                       "application_log" if record.name.startswith("app.") else "library_log")
    owner_data = _owned_records.get(record)
    owner = owner_data[0] if owner_data else trace_api.get_current_span()
    facts = {}
    if owner_data:
        facts.update(project_log_attributes(owner_data[1]))
    elif _execution_context_snapshot is not None:
        try:
            facts.update(project_log_attributes(dict(_execution_context_snapshot())))
        except Exception:
            facts["nkb.observation.error"] = "details_unavailable"
    snapshot = owner_data[2] if owner_data else _call("details_snapshot")
    details = _call("validate", snapshot)
    if getattr(snapshot,"details_unavailable",False):
        facts["nkb.observation.error"] = "details_unavailable"
    if getattr(details,"details_unavailable",False):
        facts["nkb.observation.error"] = "details_unavailable"
    standard = logging.LogRecord("",0,"",0,"",(),None).__dict__
    reserved = {"message", "asctime", "event", "http_facts", "trace_id", "span_id",
                "service.name", "service.instance.id"}
    extra = {k:v for k,v in vars(record).items() if type(k) is str and k not in standard
             and k not in reserved and not k.startswith("_")}
    if type(record.msg) is not str and record.msg is not None and not (
            type(record.msg) in (dict,list,tuple) and not record.msg):
        extra["structured_message"] = record.msg
    if extra:
        projected = _call("project", log_fields=extra)
        details.update(projected)
        if getattr(projected,"details_unavailable",False):
            facts["nkb.observation.error"] = "details_unavailable"
    message = _sanitize_content(_safe_message(record))
    if message is None and record.levelno >= logging.WARNING:
        facts["nkb.observation.error"] = "details_unavailable"
    if message is not None:
        message = _clip(message, _content_max_bytes)
    exception = _safe_exception(record.exc_info)
    message, exception, details = _fit_content(message, exception, details, facts)
    attributes = dict(facts)
    attributes.update(_call("flatten", details))
    if message:
        attributes["message"] = message
    if exception:
        attributes.update({"exception."+k:v for k,v in exception.items() if v is not None})
    logger_name = _sanitize_content(record.name) if type(record.name) is str and len(record.name) <= 512 else None
    if version != (_projection_generation, id(_sanitizer), getattr(_sanitizer,"policy_version",None)):
        # A mutable startup policy was changed during projection. A record
        # must never combine two catalogs; only fixed facts survive this race.
        message, exception, details = None, None, {}
        facts["nkb.observation.error"] = "details_unavailable"
        attributes = dict(facts)
    trace_id = span_id = None
    try:
        context = owner.get_span_context()
        if context.is_valid:
            trace_id, span_id = format(context.trace_id,"032x"), format(context.span_id,"016x")
    except Exception:
        pass
    dropped = _is_library_noise(record) or (not message and not exception and not details and not facts and record.levelno < logging.WARNING)
    prepared = Prepared(event, message, _exception_type_of(record.exc_info), details, facts,
                        exception, attributes, owner, trace_id, span_id, dropped,
                        logger_name or "[logger unavailable]")
    _prepared_records[record] = (version, prepared)
    return prepared


def _envelopes_from_flat(attributes):
    result = {}
    for name in detail_policy.FIELDS:
        prefix = "nkb.content." + name
        if type(attributes.get(prefix)) is str:
            result[name] = {"content": attributes[prefix]}
            for key in ("original_bytes", "truncated", "incomplete", "observed_bytes"):
                if prefix+"_"+key in attributes:
                    result[name][key] = attributes[prefix+"_"+key]
    return result


@contextmanager
def completion_scope(attributes):
    """Composition grants a final immutable lifecycle projection one write scope."""
    token = _prepared_outlet.set(attributes)
    try:
        yield
    finally:
        _prepared_outlet.reset(token)


def emit_completion_snapshot(snapshot):
    """Log only: sole lifecycle snapshot trusted through a private side table."""
    try:
        logger = logging.getLogger(_OBS_LOGGER_NAME)
        level = logging.WARNING if any(key.endswith(".outcome") and value in ("failure", "timed_out")
                                      for key,value in snapshot.attributes.items()) else logging.INFO
        record = logging.LogRecord(_OBS_LOGGER_NAME, level, "", 0, snapshot.event, (), None)
        _completion_records[record] = snapshot
        logger.handle(record)
    except Exception:
        pass


def _is_empty_unknown(prepared, record):
    return prepared.dropped


# ---------------------------------------------------------------------------
# 访问事实日志（SERVER/CLIENT span 仍 current 时发送）
# ---------------------------------------------------------------------------


def _safe_status(status):
    if type(status) is int and 100 <= status <= 599:
        return status
    return None


def _safe_duration(duration_ms):
    if type(duration_ms) not in (int, float) or isinstance(duration_ms, bool):
        return None
    value = float(duration_ms)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def log_http_request_completed(method, route, status, duration_ms, outcome, *,
                               details=None, owner_context=None, execution_facts=None) -> dict:
    """入站请求事实：在对应 SERVER span 仍 current 时调用一次。"""
    facts = {"nkb.http.direction": "server", "nkb.http.summary": "HTTP completed", "nkb.http.caller": "http.request"}
    outcome = {"completed": "success", "error": "failure", "incomplete": "unknown"}.get(outcome, outcome)
    if type(method) is str and method:
        facts["http.request.method"] = method
    if type(route) is str and route:
        facts["http.route"] = sanitize_text(route)[:512]
    status_value = _safe_status(status)
    if status_value is not None:
        facts["http.response.status_code"] = status_value
    duration_value = _safe_duration(duration_ms)
    if duration_value is not None:
        facts["nkb.http.duration_ms"] = duration_value
    if outcome in _OUTCOMES:
        facts["nkb.http.outcome"] = outcome
    facts.update(project_log_attributes(execution_facts or {}))
    return _emit_facts(HTTP_COMPLETED_EVENT, facts, details=details, owner_context=owner_context)


def log_http_client_completed(method, host, port, template, status, caller,
                              duration_ms=None, outcome="completed", *,
                              details=None, owner_context=None, execution_facts=None) -> dict:
    """出站请求事实：在对应 CLIENT span 真实终态时调用一次。"""
    facts = {"nkb.http.direction": "client", "nkb.http.summary": "HTTP completed"}
    outcome = {"completed": "success", "error": "failure", "incomplete": "unknown"}.get(outcome, outcome)
    if type(method) is str and method:
        facts["http.request.method"] = method
    if type(host) is str and host:
        facts["server.address"] = host
    if type(port) is int and 0 < port <= 65535:
        facts["server.port"] = port
    if type(template) is str and template:
        facts["url.template"] = sanitize_text(template)[:512]
    status_value = _safe_status(status)
    if status_value is not None:
        facts["http.response.status_code"] = status_value
    if type(caller) is str and caller:
        facts["nkb.http.caller"] = sanitize_text(caller)[:256]
    duration_value = _safe_duration(duration_ms)
    if duration_value is not None:
        facts["nkb.http.duration_ms"] = duration_value
    if outcome in _OUTCOMES:
        facts["nkb.http.outcome"] = outcome
    facts.update(project_log_attributes(execution_facts or {}))
    return _emit_facts(HTTP_CLIENT_COMPLETED_EVENT, facts, details=details, owner_context=owner_context)


def _emit_facts(event: str, facts: dict, *, details=None, owner_context=None) -> None:
    """以内置事件模板发送事实日志；正文摘要脱敏（未装配时仅 JSON 事实）。"""
    try:
        logger = logging.getLogger(_OBS_LOGGER_NAME)
        parts = []
        for key in ("http.request.method", "http.route", "url.template",
                    "http.response.status_code", "outcome"):
            value = facts.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        summary = " ".join(parts)
        return _emit_owned(event + " %s", ("HTTP completed",), facts=facts, details=details,
                    owner_context=owner_context, event_published=True)
    except Exception:
        return


def _emit_owned(template, args, *, facts=None, details=None, owner_context=None, level=logging.INFO, event_published=False):
    logger = logging.getLogger(_OBS_LOGGER_NAME)
    owner = owner_context if owner_context is not None else trace_api.get_current_span()
    if not isinstance(owner, trace_api.Span):
        owner = trace_api.get_current_span(owner)
    record = logging.LogRecord(_OBS_LOGGER_NAME, level, "", 0, template, args, None)
    # This private side table, never LogRecord.extra, establishes ownership.
    snapshot = details if details is not None else _call("details_snapshot")
    _owned_records[record] = (owner, dict(facts or {}), snapshot)
    if event_published:
        _published_records.add(record)
    prepared = project_record(record)
    if logger.isEnabledFor(level):
        logger.handle(record)
    return dict(prepared.attributes)


def emit_terminal_event(event, *, facts=None, details=None, owner_context=None, level=logging.INFO):
    """Attempt one Event/Log for an exact registered code, while owner is active.

    The operation owns its once guard. `details` is a safe-envelope snapshot,
    revalidated here; `owner_context` accepts an API Span or OTel Context.
    No provider/sample decisions are changed. Failures never affect business.
    """
    try:
        if not is_registered_event(event):
            return
        # Internal emission bypasses caller formatting: only a fixed summary.
        record_template = event + " %s"
        register_safe_event(record_template, event)
        _emit_owned(record_template, (event,), facts=facts, details=details,
                    owner_context=owner_context, level=level)
    except Exception:
        return


# ---------------------------------------------------------------------------
# JSON stderr 出口
# ---------------------------------------------------------------------------


def _service_identity():
    try:
        from .setup import SERVICE_NAME, get_runtime_handle

        handle = get_runtime_handle()
        if handle is not None:
            return SERVICE_NAME, handle.instance_id
    except Exception:
        pass
    return "n-kb", None


def _utc_timestamp(record) -> str:
    try:
        return datetime.fromtimestamp(
            getattr(record, "created", 0.0) or 0.0, tz=timezone.utc
        ).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


class SafeEventJsonFormatter(logging.Formatter):
    """固定键 JSON 行格式（stderr 出口）；内容全部经采集前投影。"""

    def format(self, record) -> str:
        prepared = project_record(record)
        if _is_empty_unknown(prepared, record):
            return ""
        event = prepared.event
        if event == UNKNOWN_EVENT_CODE:
            event = DIAGNOSTIC_EVENT_CODE
        trace_id, span_id = prepared.trace_id, prepared.span_id
        service_name, instance_id = _service_identity()
        payload = {
            "timestamp": _utc_timestamp(record),
            "severity": logging.getLevelName(
                getattr(record, "levelno", logging.INFO) or logging.INFO
            ),
            "logger": prepared.logger_name,
            "event": event,
            "message": prepared.message,
        }
        if prepared.exception_type is not None:
            payload["exception"] = prepared.exception or {"type": prepared.exception_type}
        if trace_id is not None:
            payload["trace_id"] = trace_id
        if span_id is not None:
            payload["span_id"] = span_id
        payload["service.name"] = service_name
        if instance_id is not None:
            payload["service.instance.id"] = instance_id
        payload.update(prepared.facts)
        if prepared.details:
            payload["details"] = prepared.details
        try:
            return json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except Exception:
            # 投影失败时不输出任何内容（fail closed）。
            return ""


class SafeLogFilter(logging.Filter):
    """过滤空 unknown 记录；不改写共享 record（复用一次 Prepared）。"""

    def filter(self, record) -> bool:
        try:
            prepared = project_record(record)
        except Exception:
            return False
        return not _is_empty_unknown(prepared, record)


class ObservabilityJsonHandler(logging.StreamHandler):
    """模块自有 JSON stderr handler（stdout 零日志）。"""

    _nkb_observability_owned = True

    def __init__(self, stream=None):
        super().__init__(stream if stream is not None else sys.stderr)
        self.setFormatter(SafeEventJsonFormatter())

    def emit(self, record) -> None:
        if _json_active.get():
            _safe_diagnostic()
            return
        token = _json_active.set(True)
        try:
            prepared = project_record(record)
            if _is_empty_unknown(prepared, record):
                return
            message = self.format(record)
            if not message:
                return
            self.stream.write(message + self.terminator)
            self.flush()
        except Exception:
            # Never invoke logging.handleError: it renders the raw record.
            _safe_diagnostic()
        finally:
            _json_active.reset(token)


# ---------------------------------------------------------------------------
# OTel 桥（默认无注入时仅投影 + span 事件；注入 otel_handler 后双出口）
# ---------------------------------------------------------------------------


def _is_self_traffic(name) -> bool:
    if type(name) is not str:
        return False
    return any(name == prefix or name.startswith(prefix + ".")
               for prefix in _SELF_TRAFFIC_PREFIXES)


class SafeLogBridge(logging.Handler):
    """投影后转发 OTel LoggingHandler；防递归、逐臂隔离、span 事件补投。"""

    _nkb_observability_owned = True

    def __init__(self, otel_handler=None, level=logging.INFO, on_record=None):
        super().__init__(level=level)
        self._otel_handler = otel_handler
        self._failure_count = 0
        self._on_record = on_record

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def emit(self, record) -> None:
        if _is_self_traffic(getattr(record, "name", None)) or _bridge_active.get():
            return
        token = _bridge_active.set(True)
        try:
            self._emit_safe(record)
        finally:
            _bridge_active.reset(token)

    def _emit_safe(self, record):
        try:
            prepared = project_record(record)
        except Exception:
            self._failure_count += 1
            return
        if _is_empty_unknown(prepared, record):
            return
        if self._on_record is not None:
            try:
                trace_id, span_id = prepared.trace_id, prepared.span_id
                self._on_record({
                    "timestamp": _utc_timestamp(record),
                    "severity": logging.getLevelName(record.levelno),
                    "event": prepared.event if prepared.event != UNKNOWN_EVENT_CODE else DIAGNOSTIC_EVENT_CODE,
                    "trace_id": trace_id, "span_id": span_id,
                    "attributes": dict(prepared.facts),
                })
            except Exception:
                self._failure_count += 1
        if record not in _completion_records and record not in _published_records:
            self._add_event_on_current_span(record, prepared)
        self._forward_to_otel(record, prepared)

    def _add_event_on_current_span(self, record, prepared) -> None:
        template = record.msg
        event_name = event_code_for_template(template) if type(template) is str else None
        if event_name is None or event_name in _LIFECYCLE_EVENTS:
            return
        try:
            span = prepared.owner
            if span is not None and span.is_recording():
                details = {key:value for key,value in prepared.attributes.items() if key in _DETAIL_ATTRIBUTE_KEYS}
                token = _prepared_outlet.set(details)
                try:
                    if details:
                        span.set_attributes(details)
                except Exception:
                    self._failure_count += 1
                finally:
                    _prepared_outlet.reset(token)
                token = _prepared_outlet.set(prepared.attributes)
                try:
                    span.add_event(event_name, prepared.attributes)
                finally:
                    _prepared_outlet.reset(token)
        except Exception:
            self._failure_count += 1

    def _forward_to_otel(self, record, prepared) -> None:
        handler = self._otel_handler
        if handler is None:
            return
        try:
            projected = logging.LogRecord(
                prepared.logger_name, record.levelno, "", 0,
                prepared.message or (prepared.event if prepared.event != UNKNOWN_EVENT_CODE else DIAGNOSTIC_EVENT_CODE),
                (), None,
            )
            projected.args = ()
            projected.exc_info = None
            projected.exc_text = None
            projected.stack_info = None
            projected.event = prepared.event
            projected.__dict__.update(prepared.attributes)
            # LoggingHandler obtains trusted identity from an explicit span context.
            with trace_api.use_span(prepared.owner, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                handler.handle(projected)
        except Exception:
            self._failure_count += 1


# ---------------------------------------------------------------------------
# root 装配与回收（只动本模块自有 handler）
# ---------------------------------------------------------------------------


def _owned_handlers(target) -> list:
    return [h for h in target.handlers if getattr(h, _OWNED_ATTR, False) is True]


def attach_observability_log_handlers(*, root=None, bridge=None) -> bool:
    """向 root（或指定 logger）添加本模块自有的一套 handler（幂等）。

    既有 handler 不改写；存在外部 handler 时输出一次边界诊断（直接写
    stderr，不走 logging，防递归）。``bridge`` 为测试/组合根注入点。
    本模块拥有事实 logger，装配时确保其级别不低于 INFO（默认 root
    WARNING 会把 http_request_completed 等事实静默吞掉）。
    """
    global _boundary_warned
    target = root if root is not None else logging.getLogger()
    try:
        if not _owned_handlers(target):
            target.addHandler(ObservabilityJsonHandler())
            target.addHandler(
                bridge if bridge is not None else SafeLogBridge()
            )
        facts_logger = logging.getLogger(__name__)
        if facts_logger.getEffectiveLevel() > logging.INFO:
            facts_logger.setLevel(logging.INFO)
        foreign = [h for h in target.handlers if getattr(h, _OWNED_ATTR, False) is not True]
        if foreign and not _boundary_warned:
            _boundary_warned = True
            sys.stderr.write(
                "n-kb observability: logger '%s' has %d existing handler(s); "
                "owned JSON stderr handler and OTel bridge attached alongside "
                "(boundary diagnostic).\n" % (getattr(target, "name", "?"), len(foreign))
            )
            try:
                sys.stderr.flush()
            except Exception:
                pass
        return True
    except Exception:
        return False


def detach_observability_log_handlers(*, root=None) -> int:
    """只移除本模块自有 handler；返回移除数量。"""
    target = root if root is not None else logging.getLogger()
    removed = 0
    for handler in list(target.handlers):
        if getattr(handler, _OWNED_ATTR, False) is True:
            try:
                target.removeHandler(handler)
                removed += 1
            except Exception:
                continue
    return removed


def _reset_for_tests() -> None:
    """测试隔离：清空注册表/边界标记/sanitizer（重建内置注册）。"""
    global _sanitizer, _boundary_warned
    with _registry_lock:
        _reset_registry()
    _sanitizer = None
    _owned_records.clear()
    _prepared_records.clear()
    _boundary_warned = False
    configure_execution_context()


__all__ = [
    "UNKNOWN_EVENT_CODE",
    "DIAGNOSTIC_EVENT_CODE",
    "HTTP_COMPLETED_EVENT",
    "HTTP_CLIENT_COMPLETED_EVENT",
    "LogSanitizer",
    "Prepared",
    "SafeEventJsonFormatter",
    "SafeLogFilter",
    "ObservabilityJsonHandler",
    "SafeLogBridge",
    "register_safe_event",
    "event_code_for_template",
    "event_code_for",
    "project_record",
    "configure_execution_context",
    "emit_terminal_event",
    "log_http_request_completed",
    "log_http_client_completed",
    "prepare_sanitizer",
    "ensure_sanitizer",
    "configure_secret_catalog",
    "sanitize_text",
    "attach_observability_log_handlers",
    "detach_observability_log_handlers",
]
