"""Infrastructure text redaction and injected detail-policy facade.

LogSanitizer owns credential/catalog/media text redaction. The composition root
injects Application's single bounded envelope policy through bind_details;
this module never imports Application. Unbound detail calls fail closed.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Callable, Pattern

REDACTED = "[REDACTED]"
REDACTED_MEDIA = "[REDACTED MEDIA]"


# ---------------------------------------------------------------------------
# 第一部分：内容脱敏器（移植 N-Agent infrastructure/observability/sanitizer.py）
# ---------------------------------------------------------------------------

Matcher = Callable[[re.Match], str]


@dataclass(frozen=True)
class Rule:
    pattern: Pattern[str]
    replacer: Matcher

    @property
    def is_default(self) -> bool:
        return self._in_default

    _in_default: bool = False


def _redact_group(group_index: int) -> Matcher:
    """Build a replacer that redacts exactly one captured group, keeping all
    other matched text (e.g. field name/separator/scheme/@).

    ``re`` passes the replacer the whole Match and swaps the returned string
    for the *matched span*, so offsets are computed relative to ``span(0)``."""

    def replacer(match: re.Match) -> str:
        whole_start = match.start(0)
        start, end = match.span(group_index)
        token = match.group(0)
        rel_start = start - whole_start
        rel_end = end - whole_start
        return token[:rel_start] + REDACTED + token[rel_end:]

    return replacer


def _redact_last_group(match: re.Match) -> str:
    """Redact the last (by position) captured group; a group-less match
    redacts the whole token."""
    whole_start = match.start(0)
    token = match.group(0)
    for group_index in range(len(match.groups()), 0, -1):
        start, end = match.span(group_index)
        if start >= 0:
            return token[: start - whole_start] + REDACTED + token[end - whole_start :]
    return REDACTED


# Authorization header: keep "Bearer "/"Basic ", redact the credential token.
_PAT_AUTHORIZATION = re.compile(
    r"(?i)(\bBearer\s+|\bBasic\s+)([A-Za-z0-9\-._~+/]+=*)"
)
# URL userinfo: scheme://user:pass@host -> scheme://[REDACTED]@host
_PAT_URL_USERINFO = re.compile(
    r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)([^/\r\n\s@]+)(@)"
)
# Credential fields: keep name + separator, redact value.
_PAT_CREDENTIAL_QUOTED = re.compile(
    r'''(?i)((?P<key_quote>["'])[\w.-]*(?:api[_-]?key|apikey|token|password|passwd|pwd|secret)[\w.-]*(?P=key_quote))'''
    r'''(\s*:\s*)(?:(?P<value_quote>["'])((?:\\(?:[\s\S]|$)|(?!(?P=value_quote))[^\\])*)(?:(?P=value_quote)|$)|([^\s,;}]+))'''
)
_PAT_CREDENTIAL_RAW = re.compile(
    r"(?i)(?<![\w.-])([\w.-]*(?:api[_-]?key|apikey|token|password|passwd|pwd|secret)"
    r"[\w.-]*)(\s*[:=]\s*|\s*:\s*)([^\s,\";&]+)"
)
# JWT: three base64url segments (no capture group -> whole match redacted).
_PAT_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\."
    r"[A-Za-z0-9_-]{4,}(?![A-Za-z0-9])"
)
# PEM private key block (multi-line), no group -> whole match redacted.
_PAT_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

_DEFAULT_RULES = (
    Rule(_PAT_AUTHORIZATION, _redact_group(2)),
    Rule(_PAT_URL_USERINFO, _redact_group(2)),
    Rule(_PAT_CREDENTIAL_RAW, _redact_group(3)),
    Rule(_PAT_CREDENTIAL_QUOTED, _redact_last_group),
    Rule(_PAT_JWT, _redact_last_group),
    Rule(_PAT_PEM, _redact_last_group),
)


class LogSanitizer:
    """Immutable-version, thread-safe content sanitizer.

    Each ``sanitize`` builds a frozen snapshot (rules tuple + known secrets
    frozenset) under a lock, so a concurrent ``register_scrub_rule`` /
    ``configure_secret_catalog`` never produces a half-versioned result within
    one call. ``register_scrub_rule`` is a trusted-startup extension point
    that never accepts untrusted input.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[Rule] = [
            Rule(r.pattern, r.replacer, _in_default=True) for r in _DEFAULT_RULES
        ]
        self._known_secrets: frozenset[str] = frozenset()
        self._policy_version = 0

    # -- configuration ------------------------------------------------------

    def register_scrub_rule(self, pattern: str | Pattern[str]) -> None:
        """Append a fixed sensitive pattern (trusted startup). Custom rules
        redact their whole matches."""
        if isinstance(pattern, Pattern):
            compiled = pattern
        elif isinstance(pattern, str):
            compiled = re.compile(pattern)
        else:
            raise TypeError("scrub rule must be a str or compiled re.Pattern")
        with self._lock:
            self._rules.append(Rule(compiled, _redact_last_group))
            self._policy_version += 1

    def configure_secret_catalog(self, known_secrets: object) -> None:
        """Atomically publish an immutable set of known secret values.

        Empty strings are never secrets. An empty catalog is valid and still
        runs the fixed-pattern rules."""
        secrets = frozenset(
            v for v in (known_secrets or ()) if isinstance(v, str) and v
        )
        with self._lock:
            self._known_secrets = secrets
            self._policy_version += 1

    @property
    def policy_version(self) -> int:
        """Version used to invalidate cached envelope metadata after policy edits."""
        with self._lock:
            return self._policy_version

    # -- applies ------------------------------------------------------------

    def sanitize(self, text: str) -> str:
        """Redact known secrets and fixed sensitive patterns in ``text``.

        ``text`` must be str; non-str returns unchanged (callers validate
        types before invoking). Replacement output does not re-enter pattern
        matching, so a short known secret cannot corrupt an emitted
        ``[REDACTED]`` placeholder."""
        if not isinstance(text, str):
            return text
        with self._lock:
            rules = tuple(self._rules)
            secrets = self._known_secrets
        out = self._redact_secrets(text, secrets)
        for rule in rules:
            out = rule.pattern.sub(rule.replacer, out)
        return out

    def sanitize_partial(self, text: str) -> str:
        """Fail closed for an incomplete raw prefix, including encoded tails.

        No unclosed token/JSON/form segment is provably complete. Omitting the
        prefix avoids exposing a catalog secret split at any byte boundary.
        """
        return "[DETAILS INCOMPLETE]"

    @staticmethod
    def _redact_secrets(text: str, secrets: frozenset[str]) -> str:
        if not secrets:
            return text
        # Single left-to-right pass, longest value wins at a given start;
        # replacement output is not rescanned for secrets.
        result = []
        i = 0
        n = len(text)
        while i < n:
            best_end = -1
            best_len = -1
            for secret in secrets:
                ln = len(secret)
                if ln <= best_len:
                    continue
                if text.startswith(secret, i):
                    best_end = i + ln
                    best_len = ln
            if best_end >= 0:
                result.append(REDACTED)
                i = best_end
            else:
                result.append(text[i])
                i += 1
        return "".join(result)


# ---------------------------------------------------------------------------
# 第二部分：采集前投影（project_details）
# ---------------------------------------------------------------------------

# Exact approved payload keys; legacy facts migrate inside the injected policy.
FIELDS = frozenset(("retrieval_input", "retrieval_output", "document_input",
    "index_input", "index_result", "embedding_input", "embedding_result",
    "http_request", "http_response", "error", "log_fields"))
RECORD_TRUNCATED = "[RECORD TRUNCATED]"
ITEMS_TRUNCATED = "[truncated]"
_callbacks = None
_pending_config = (None, 1024000, None)


def bind_details(*, configure, project, validate, flatten):
    """Composition injects the sole policy; infrastructure never imports it."""
    global _callbacks
    _callbacks = (configure, project, validate, flatten)
    configure(*_pending_config)


def clear_details_bindings():
    """Release only our injected callbacks after the final runtime reference."""
    global _callbacks
    _callbacks = None


def configure_details(sanitizer, max_bytes=1024000, max_total_bytes=None):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if max_total_bytes is not None and (type(max_total_bytes) is not int or max_total_bytes <= 0):
        raise ValueError("max_total_bytes must be a positive integer")
    if sanitizer is not None and not callable(sanitizer):
        raise ValueError("sanitizer must be callable or None")
    global _pending_config
    _pending_config = (sanitizer, max_bytes, max_total_bytes)
    if _callbacks:
        _callbacks[0](*_pending_config)


def project_details(**fields):
    return _callbacks[1](**fields) if _callbacks else {}


def validate_details(details):
    return _callbacks[2](details) if _callbacks else {}


def flatten_details(details, prefix="nkb.content."):
    return _callbacks[3](details, prefix=prefix) if _callbacks else {}
