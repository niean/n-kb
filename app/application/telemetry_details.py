"""Bounded, fail-closed detail envelopes and operation-local safe snapshots.

The composition root supplies the text sanitizer. This module uses stdlib only.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import json
import math
import re

FIELDS = frozenset(("retrieval_input", "retrieval_output", "document_input",
    "index_input", "index_result", "embedding_input", "embedding_result",
    "http_request", "http_response", "error", "log_fields"))
_METADATA = frozenset(("content", "original_bytes", "truncated", "incomplete", "observed_bytes"))
_SENSITIVE = re.compile(r"api.?key|apikey|token|password|passwd|pwd|authorization|cookie|credential|secret|private.?key", re.I)
_TOKEN_COUNTS = frozenset(("max_tokens", "input_tokens", "output_tokens", "total_tokens", "token_count", "cached_tokens"))
_RESTRICTED = frozenset(("tags", "sensitive_tags", "file_content", "upload_content", "document_text", "full_text", "raw", "documents", "document", "chunk_text", "vector", "vectors", "embedding", "embeddings", "sql", "statement", "parameters", "params", "db.statement", "db.query.text"))
_MEDIA_KEYS = frozenset(("base64", "b64_json", "image_data", "audio_data"))
_MEDIA_TYPES = frozenset(("base64", "image", "image_url", "input_image", "input_audio", "audio"))
_config = (None, 1024000, 4096000)
_config_version = 0
_context = ContextVar("nkb_safe_details", default=None)


def configure_details(sanitizer, max_bytes=1024000, max_total_bytes=None):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if max_total_bytes is None:
        max_total_bytes = 4 * max_bytes
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be a positive integer")
    if sanitizer is not None and not callable(sanitizer):
        raise ValueError("sanitizer must be callable or None")
    global _config, _config_version
    _config = (sanitizer, max_bytes, max_total_bytes)
    _config_version += 1


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _truncate(text, limit):
    return text[:limit].encode("utf-8")[:limit].decode("utf-8", errors="ignore")


class _Budget:
    def __init__(self, sanitizer, limit):
        self.sanitizer = sanitizer
        self.nodes = 4096
        self.scan = max(8192, 16 * limit)
        self.incomplete = False
        self.active = set()

    def sanitize(self, text):
        out = self.sanitizer(text)
        if type(out) is not str:
            raise ValueError("invalid sanitizer output")
        out.encode("utf-8")
        return out

    def text(self, text, depth=0):
        # Bound bytes before parsing, regexes or encoding arbitrarily large input.
        prefix = text[:self.scan]
        raw = prefix.encode("utf-8")
        full = len(prefix) == len(text) and len(raw) <= self.scan
        raw = raw[:self.scan]
        prefix = raw.decode("utf-8", errors="ignore")
        self.scan -= len(raw)
        if not full:
            self.incomplete = True
            owner = getattr(self.sanitizer, "__self__", None)
            partial = getattr(owner, "sanitize_partial", None)
            # A generic callback cannot prove that a captured secret prefix is safe.
            return partial(prefix) if partial else "[DETAILS INCOMPLETE]"
        if prefix.lower().startswith("data:") and ";base64," in prefix[:256].lower():
            return "[REDACTED MEDIA]"
        if prefix.lstrip().startswith(("{", "[")):
            try:
                # Parse only bounded strings and refuse parser recursion bombs.
                parsed = json.loads(prefix)
            except (ValueError, RecursionError):
                pass
            else:
                return _dump(self.clean(parsed, depth + 1))
        return self.sanitize(prefix)

    def clean(self, value, depth=0):
        self.nodes -= 1
        if self.nodes < 0 or depth > 64:
            self.incomplete = True
            return "[truncated]"
        kind = type(value)
        if kind is str:
            return self.text(value, depth)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[REDACTED MEDIA]"
        if value is None or kind in (bool, int, float):
            if kind is float and not math.isfinite(value):
                raise ValueError("nonfinite value")
            if kind is int and value.bit_length() > 4096:
                self.incomplete = True
                return "[truncated]"
            text = _dump(value)
            safe = self.sanitize(text)
            return value if safe == text else safe
        if kind not in (dict, list, tuple):
            return self.sanitize("[unsupported value: " + type(value).__name__[:64] + "]")
        if id(value) in self.active:
            self.incomplete = True
            return "[cycle]"
        self.active.add(id(value))
        try:
            if kind in (list, tuple):
                out = []
                for i, item in enumerate(value):
                    if i >= 128 or self.nodes <= 0:
                        self.incomplete = True
                        out.append("[truncated]")
                        break
                    out.append(self.clean(item, depth + 1))
                return out
            media_type = value.get("type")
            if type(media_type) is str and media_type in _MEDIA_TYPES:
                return {"media": "[REDACTED MEDIA]"}
            out = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= 128 or self.nodes <= 0:
                    self.incomplete = True
                    out["[truncated]"] = True
                    break
                unobservable_key = False
                if type(key) is str:
                    unobservable_key = len(key) > 256
                    name = key[:256]
                    if len(key) > 256:
                        self.incomplete = True
                elif type(key) in (int, float, bool) or key is None:
                    name = _dump(key)
                else:
                    name = "[unsupported key]"
                lower = name.lower()
                scan_before = self.scan
                name = self.text(name, depth + 1)
                if type(key) is str and len(key[:256].encode("utf-8")) > scan_before:
                    unobservable_key = True
                if unobservable_key:
                    self.incomplete = True
                    # A truncated key can itself end inside a known secret.
                    # Never publish its raw prefix as a supposedly safe name.
                    out["[DETAIL KEY UNAVAILABLE]"] = "[REDACTED]"
                elif lower in _MEDIA_KEYS:
                    out[name] = "[REDACTED MEDIA]"
                elif lower in _RESTRICTED or (_SENSITIVE.search(lower) and not (lower in _TOKEN_COUNTS and type(item) in (int,float))):
                    out[name] = "[REDACTED]"
                else:
                    out[name] = self.clean(item, depth + 1)
            return out
        finally:
            self.active.remove(id(value))


def _priority(name):
    return (0 if name == "error" else 3 if name == "log_fields" else
            1 if name.endswith("_input") or name in ("document_input", "http_request") else 2, name)


def _legacy(fields):
    """Explicit compatibility mapping; never relax the approved top-level enum."""
    result = {k:v for k,v in fields.items() if k in FIELDS}
    legacy = {k:fields[k] for k in ("stage", "stage_results", "counts", "sizes") if k in fields}
    if "sqlite" in fields and type(fields["sqlite"]) is dict:
        legacy["sqlite"] = {k:v for k,v in fields["sqlite"].items() if k in ("operation", "table")}
    if legacy:
        existing = result.get("index_result")
        result["index_result"] = dict(existing, **legacy) if type(existing) is dict else legacy
    if "target" in fields and type(fields["target"]) is dict:
        target = {k:v for k,v in fields["target"].items() if k in ("scheme", "host", "port")}
        existing = result.get("http_request")
        result["http_request"] = dict(existing, target=target) if type(existing) is dict else {"target":target}
    return result


class Details(dict):
    """Dictionary-compatible result with fixed diagnostics outside payload keys."""
    details_unavailable = False


def _policy_version():
    owner = getattr(_config[0], "__self__", None)
    return getattr(owner, "policy_version", None)


class _Envelope(dict):
    def seal(self):
        self._policy_config = _config_version
        self._policy = _policy_version()
        self._signature = tuple(sorted(self.items()))
        return self

    def trusted(self):
        return (getattr(self, "_policy_config", None) == _config_version and
                getattr(self, "_policy", None) == _policy_version() and
                getattr(self, "_signature", None) == tuple(sorted(self.items())))


def budget_details(details, max_total_bytes):
    """Fit safe envelopes into the caller's remaining serialized record budget.

    Callers reserve their message/exception bytes first, including separators.
    The budget includes the details object itself and all envelope metadata.
    """
    if type(max_total_bytes) is not int or max_total_bytes < 0:
        raise ValueError("max_total_bytes must be nonnegative")
    result = Details()
    for name, source in sorted(details.items(), key=lambda item:_priority(item[0])):
        if name not in FIELDS:
            continue
        item = _Envelope(source)
        while True:
            candidate = dict(result, **{name:item})
            excess = len(_dump(candidate).encode("utf-8")) - max_total_bytes
            if excess <= 0:
                result[name] = item.seal()
                break
            if not item["content"]:
                result.details_unavailable = True
                break
            item["content"] = _truncate(item["content"], max(0, len(item["content"].encode("utf-8"))-max(1,excess)))
            item["truncated"] = True
    result.details_unavailable = result.details_unavailable or getattr(details, "details_unavailable", False)
    return result


def project_details(**fields):
    sanitizer, limit, total = _config
    result = Details()
    if sanitizer is None:
        result.details_unavailable = bool(fields)
        return result
    budget = _Budget(sanitizer, limit)
    for name, value in sorted(_legacy(fields).items(), key=lambda item:_priority(item[0])):
        try:
            budget.sanitize("")
            budget.incomplete = False
            clean = budget.clean(value)
            text = clean if type(clean) is str else _dump(clean)
            size = len(text.encode("utf-8"))
            item = dict(content=_truncate(text, limit), original_bytes=None if budget.incomplete else size,
                        truncated=budget.incomplete or size > limit)
            if budget.incomplete:
                item["incomplete"] = True
            result[name] = _Envelope(item).seal()
        except Exception:
            result.details_unavailable = True
    return budget_details(result, total)


def validate_details(details):
    if not isinstance(details, dict):
        return Details()
    raw = {}
    metadata = {}
    for name, item in details.items():
        if name not in FIELDS or type(item) not in (dict, _Envelope) or set(item) - _METADATA:
            continue
        if not {"content", "original_bytes", "truncated"} <= item.keys():
            continue
        if type(item["content"]) is not str or type(item["truncated"]) is not bool:
            continue
        size = item["original_bytes"]
        if size is not None and (type(size) is not int or size < 0):
            continue
        if "incomplete" in item and type(item["incomplete"]) is not bool:
            continue
        if "observed_bytes" in item and (type(item["observed_bytes"]) is not int or item["observed_bytes"] < 0):
            continue
        raw[name] = item["content"]
        metadata[name] = item
    # Internally produced, unmodified envelopes preserve known full sizes.
    # Raw dictionaries from external outlets always undergo a fresh projection.
    result = project_details(**raw)
    for name, item in list(result.items()):
        prior = metadata[name]
        if (type(prior) is _Envelope and prior.trusted()
                and prior["content"] == item["content"]):
            # Fresh content always wins. Only metadata from an unchanged policy
            # and unchanged safe prefix can retain its known complete size.
            for key in ("original_bytes", "truncated", "incomplete", "observed_bytes"):
                if key in prior:
                    item[key] = prior[key]
            item.seal()
            continue
        if prior["truncated"] or prior.get("incomplete") or prior["original_bytes"] is None:
            item.update(original_bytes=None, truncated=True)
        if prior.get("incomplete"):
            item["incomplete"] = True
        if "observed_bytes" in prior:
            item["observed_bytes"] = prior["observed_bytes"]
        item.seal()
    return budget_details(result, _config[2])


def flatten_details(details, prefix="nkb.content."):
    return {(f"{prefix}{name}" if key == "content" else f"{prefix}{name}_{key}"):value for name,item in validate_details(details).items()
            for key,value in item.items() if value is not None}


def current_details():
    stack = _context.get() or ()
    return deepcopy(stack[-1][1]) if stack else {}


class _DetailScope:
    def __init__(self, fields):
        self.snapshot = project_details(**fields)
        self.token = None
        self.active = False

    def __enter__(self):
        stack = _context.get() or ()
        self.token = _context.set(stack + ((id(self), deepcopy(self.snapshot)),))
        self.active = True
        return self

    def update(self, **fields):
        stack = _context.get() or ()
        current = next((snapshot for owner, snapshot in stack if owner == id(self)), None)
        if current is None:
            return
        projected = project_details(**fields)
        # The owner object can be shared by copied async/thread contexts. Merge
        # only this context's frame; never publish through mutable owner state.
        merged = validate_details(dict(current, **projected))
        _context.set(tuple((owner, deepcopy(merged) if owner == id(self) else snapshot)
                           for owner, snapshot in stack))

    def __exit__(self, *exc):
        try:
            stack = _context.get() or ()
            parents = stack[:-1] if stack and stack[-1][0] == id(self) else ()
            _context.reset(self.token)
            # Preserve updates to an ancestor made in this same task while
            # restoring the caller's active owner. Frames contain safe copies.
            if parents:
                _context.set(parents)
        finally:
            self.active = False
            self.snapshot = {}
            self.token = None


def detail_scope(**fields):
    return _DetailScope(fields)


def update_current_details(**fields):
    """Update only the current operation's bounded safe snapshot."""
    stack = _context.get() or ()
    if not stack:
        return
    try:
        owner,current = stack[-1]
        projected = project_details(**fields)
        merged = validate_details(dict(current,**projected))
        _context.set(stack[:-1]+((owner,deepcopy(merged)),))
    except Exception:
        pass
