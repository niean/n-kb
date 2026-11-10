"""Immutable N-KB observability vocabulary. Standard library only."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
import math
import re

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
ERROR_TYPE_ATTR = "error.type"
# 成本事实键（T5 KB 管线输入，键名一经定稿不得改动）
NKB_MODEL_COST_STATUS = "nkb.model.cost_status"
NKB_MODEL_COST_REASON = "nkb.model.cost_reason"
NKB_MODEL_COST_CURRENCY = "nkb.model.cost_currency"
NKB_MODEL_COST_DECIMAL = "nkb.model.cost_decimal"
NKB_MODEL_COST_USD = "nkb.model.cost_usd"
NKB_MODEL_COST_INPUT_USD = "nkb.model.cost_input_usd"
NKB_MODEL_COST_COMPONENT = "nkb.model.cost_component"
NKB_MODEL_PRICE_ID = "nkb.model.price_id"
NKB_MODEL_PRICING_VERSION = "nkb.model.pricing_version"
NKB_MODEL_BILLING_PROVIDER = "nkb.model.billing_provider"
NKB_MODEL_BILLING_MODEL = "nkb.model.billing_model"
# 成本 Counter（单位 USD / USD / {attempt}）
INSTRUMENT_MODEL_COST = "nkb.model.cost"
INSTRUMENT_MODEL_COST_COMPONENTS = "nkb.model.cost_components"
INSTRUMENT_MODEL_COST_ATTEMPTS = "nkb.model.cost_attempts"
# 成本判定状态、八类不可估原因（与 Domain UnknownReason 词汇一致）与分项桶词汇
COST_STATUSES = frozenset(("estimated", "unknown"))
COST_REASONS = frozenset(
    (
        "missing_usage",
        "invalid_usage",
        "price_not_found",
        "price_ambiguous",
        "unsupported_pricing",
        "identity_unresolved",
        "pricing_unavailable",
        "amount_unrepresentable",
    )
)
COST_BUCKETS = frozenset(("input", "output", "cache_read", "cache_write"))
OUTCOMES = frozenset(
    (
        "success",
        "failure",
        "denied",
        "cancelled",
        "timed_out",
        "skipped",
        "deferred",
        "unknown",
    )
)
ERROR_TYPES = frozenset(
    (
        "unsupported_file_type",
        "file_too_large",
        "embedding_failed",
        "vector_index_failed",
        "sqlite_failed",
        "timeout",
        "other",
    )
)
OBJECTS = frozenset(
    (
        "document",
        "index",
        "retrieval",
        "model",
        "mcp",
        "request",
        "http",
        "observation",
        "metric",
        "content",
    )
)
INDEX_STAGES = ("parse", "split", "embed", "write")
CONTENT_FIELDS = frozenset(
    (
        "retrieval_input",
        "retrieval_output",
        "document_input",
        "index_input",
        "index_result",
        "embedding_input",
        "embedding_result",
        "http_request",
        "http_response",
        "error",
        "log_fields",
    )
)
CONTENT_METADATA = MappingProxyType(
    {
        "original_bytes": "count",
        "truncated": "bool",
        "incomplete": "bool",
        "observed_bytes": "count",
    }
)
CHANNELS = frozenset(("api", "mcp_http", "mcp_stdio", "unknown"))
DIAGNOSTICS = frozenset(
    (
        "unknown_operation",
        "invalid_attribute",
        "details_unavailable",
        "projection_failed",
    )
)
ID_PATTERN = re.compile(r"[\w.:/-]+")
MODEL_PATTERN = re.compile(r"[\w.:/@_-]+")
# 成本十进制金额字符串：规范非指数十进制（无符号、无指数、无冗余尾零），
# 整数位与小数位各 340 位，与 Domain 投影上限（COST_DECIMAL_MAX_SIDE_DIGITS）
# 和 Collector 护栏一致；超界金额在 Domain 按 amount_unrepresentable 收口，
# 不会到达本契约
COST_DECIMAL_PATTERN = re.compile(r"\d{1,340}(?:\.\d{1,340})?")
# pricing_version / price_id 的有界开放集合（受信配置词汇，不做白名单收窄）
PRICING_ID_PATTERN = re.compile(r"[\w.:/@+-]+")


@dataclass(frozen=True)
class OperationSpec:
    object: str
    action: str
    event: str
    summary: str


OPERATIONS = MappingProxyType(
    {
        **{
            name: OperationSpec(
                obj,
                action,
                ("nkb.http.completed" if obj == "request" else f"nkb.{obj}.completed"),
                f"{name} completed",
            )
            for obj, actions in (
                ("document", ("upload",)),
                ("index", ("run", *INDEX_STAGES)),
                ("retrieval", ("search",)),
                ("mcp", ("status",)),
                ("request", ("handle",)),
            )
            for action in actions
            for name in (f"{obj}.{action}",)
        },
        "embeddings": OperationSpec(
            "model", "embeddings", "nkb.model.completed", "embeddings completed"
        ),
    }
)
EVENTS = frozenset(spec.event for spec in OPERATIONS.values()) | {"nkb.http.completed"}
IDENTITY_ATTRIBUTES = frozenset(
    (
        "nkb.document.id",
        "nkb.index.document_id",
        "nkb.index.job_id",
        "nkb.request.id",
        "nkb.request.channel",
    )
)

# Types are semantic, so bool cannot accidentally pass as an int.
ATTRIBUTES = MappingProxyType(
    {
        **{
            f"nkb.{obj}.{field}": kind
            for obj in (
                "document",
                "index",
                "retrieval",
                "model",
                "mcp",
                "request",
                "http",
            )
            for field, kind in (
                ("outcome", "outcome"),
                ("summary", "summary"),
                ("duration_ms", "number"),
            )
        },
        **{
            f"nkb.{obj}.operation": "operation"
            for obj in ("document", "index", "retrieval", "mcp", "request")
        },
        **{key: "id" for key in IDENTITY_ATTRIBUTES if key != "nkb.request.channel"},
        "nkb.request.channel": "channel",
        "nkb.index.stage": "stage",
        "nkb.index.chunk_count": "count",
        "nkb.retrieval.result_count": "count",
        "nkb.retrieval.filter_tags": "count",
        "nkb.retrieval.filter_source_kind": "presence",
        "nkb.retrieval.filter_document_status": "presence",
        "nkb.retrieval.filter_min_score": "presence",
        "nkb.model.usage_known": "bool",
        NKB_MODEL_COST_STATUS: "cost_status",
        NKB_MODEL_COST_REASON: "cost_reason",
        NKB_MODEL_COST_CURRENCY: "cost_currency",
        NKB_MODEL_COST_USD: "number",
        NKB_MODEL_COST_INPUT_USD: "number",
        NKB_MODEL_COST_DECIMAL: "decimal",
        NKB_MODEL_PRICING_VERSION: "pricing_version",
        NKB_MODEL_PRICE_ID: "price_id",
        NKB_MODEL_BILLING_PROVIDER: "billing_provider",
        NKB_MODEL_BILLING_MODEL: "billing_model",
        "nkb.http.direction": "direction",
        "nkb.http.caller": "caller",
        "nkb.observation.error": "diagnostic",
        "nkb.metric.overflow": "bool",
        GEN_AI_OPERATION_NAME: "genai_operation",
        GEN_AI_REQUEST_MODEL: "model",
        GEN_AI_PROVIDER_NAME: "provider",
        GEN_AI_USAGE_INPUT_TOKENS: "count",
        GEN_AI_TOKEN_TYPE: "token_type",
        ERROR_TYPE_ATTR: "error",
        **{f"nkb.content.{field}": "text" for field in CONTENT_FIELDS},
        **{
            f"nkb.content.{field}_{suffix}": kind
            for field in CONTENT_FIELDS
            for suffix, kind in CONTENT_METADATA.items()
        },
    }
)

CONTENT_ATTRIBUTES = frozenset(
    key for key in ATTRIBUTES if key.startswith("nkb.content.")
)


# Caller text is accepted only after the composition root maps a known callsite.
HTTP_CALLERS = frozenset(
    ("ollama.embed", "qdrant.request", "health.check", "http.request", "unknown")
)


def safe_id(value):
    return (
        value
        if type(value) is str and 0 < len(value) <= 128 and ID_PATTERN.fullmatch(value)
        else None
    )


def valid_attribute(key, value):
    kind = ATTRIBUTES.get(key)
    if kind is None:
        return False
    if kind == "bool":
        return type(value) is bool
    if kind in ("count", "presence"):
        return type(value) is int and value >= 0 and (kind != "presence" or value <= 1)
    if kind == "number":
        if type(value) not in (int, float) or value < 0:
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    if kind == "decimal":
        # 成本十进制金额字符串：非负、有限、逐侧有界（681 = 340 + "." + 340）
        if type(value) is not str or not value or len(value) > 681:
            return False
        return COST_DECIMAL_PATTERN.fullmatch(value) is not None
    if kind == "pricing_version":
        return (
            type(value) is str
            and 0 < len(value) <= 129
            and PRICING_ID_PATTERN.fullmatch(value) is not None
        )
    if kind == "price_id":
        return (
            type(value) is str
            and 0 < len(value) <= 128
            and PRICING_ID_PATTERN.fullmatch(value) is not None
        )
    if kind == "billing_model":
        # 受信计费模型名：有界开放集合（不白名单化），拒绝控制字符
        if type(value) is not str or not 0 < len(value) <= 64:
            return False
        return not any(ord(char) < 32 or ord(char) == 127 for char in value)
    if type(value) is not str:
        return False
    if kind == "id":
        return safe_id(value) is not None
    if kind == "text":
        return True  # only the separate bounded detail policy admits content
    if kind == "model":
        return 0 < len(value) <= 64 and MODEL_PATTERN.fullmatch(value) is not None
    if kind == "operation":
        return any(
            s.object == key.split(".")[1] and s.action == value
            for s in OPERATIONS.values()
        )
    if kind == "summary":
        return any(
            s.object == key.split(".")[1] and s.summary == value
            for s in OPERATIONS.values()
        ) or (key == "nkb.http.summary" and value == "HTTP completed")
    enum = {
        "outcome": OUTCOMES,
        "channel": CHANNELS,
        "stage": INDEX_STAGES,
        "direction": ("server", "client"),
        "caller": HTTP_CALLERS,
        "diagnostic": DIAGNOSTICS,
        "genai_operation": ("embeddings",),
        "provider": ("ollama",),
        "token_type": ("input",),
        "error": ERROR_TYPES,
        "cost_status": COST_STATUSES,
        "cost_reason": COST_REASONS | {"none"},
        "cost_currency": ("USD",),
        "billing_provider": ("ollama", "other"),
    }
    return value in enum.get(kind, ())


def project_attributes(attributes):
    if not isinstance(attributes, Mapping):
        return {}
    return {
        key: value for key, value in attributes.items() if valid_attribute(key, value)
    }


def project_execution_attributes(attributes):
    """Project ordinary execution facts; content requires the detail policy.

    project_attributes only checks contract types and is suitable for the final
    snapshot after telemetry_details has sanitized and bounded the content.
    """
    return {
        key: value
        for key, value in project_attributes(attributes).items()
        if key not in CONTENT_ATTRIBUTES
    }


@dataclass(frozen=True)
class InstrumentSpec:
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
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    67108864,
)
GENAI_DURATION_BOUNDARIES = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
    30.0,
)
NKB_DURATION_BOUNDARIES = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
CHUNK_HISTOGRAM_BOUNDARIES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)
RESULT_HISTOGRAM_BOUNDARIES = (0, 1, 2, 3, 5, 8, 10, 20, 50, 100)

INSTRUMENTS: Mapping[str, InstrumentSpec] = MappingProxyType(
    {
        INSTRUMENT_OPERATION_DURATION: InstrumentSpec(
            "histogram",
            "s",
            GENAI_DURATION_BOUNDARIES,
            (GEN_AI_OPERATION_NAME, GEN_AI_REQUEST_MODEL, ERROR_TYPE_ATTR),
        ),
        INSTRUMENT_TOKEN_USAGE: InstrumentSpec(
            "histogram",
            "{token}",
            TOKEN_HISTOGRAM_BOUNDARIES,
            (GEN_AI_OPERATION_NAME, GEN_AI_REQUEST_MODEL, GEN_AI_TOKEN_TYPE),
        ),
        "nkb.document.uploads": InstrumentSpec(
            "counter", "1", allowed_dimensions=("nkb.document.outcome",)
        ),
        "nkb.document.upload.duration": InstrumentSpec(
            "histogram", "s", NKB_DURATION_BOUNDARIES, ("nkb.document.outcome",)
        ),
        "nkb.index.duration": InstrumentSpec(
            "histogram", "s", NKB_DURATION_BOUNDARIES, ("nkb.index.outcome",)
        ),
        "nkb.index.stage.duration": InstrumentSpec(
            "histogram",
            "s",
            NKB_DURATION_BOUNDARIES,
            ("nkb.index.stage", "nkb.index.outcome"),
        ),
        "nkb.mcp.requests": InstrumentSpec(
            "counter", "1", allowed_dimensions=("nkb.mcp.operation", "nkb.mcp.outcome")
        ),
        "nkb.mcp.duration": InstrumentSpec(
            "histogram",
            "s",
            NKB_DURATION_BOUNDARIES,
            ("nkb.mcp.operation", "nkb.mcp.outcome"),
        ),
        INSTRUMENT_INDEX_JOBS: InstrumentSpec(
            "counter", "1", allowed_dimensions=("nkb.index.outcome",)
        ),
        INSTRUMENT_INDEX_CHUNKS: InstrumentSpec(
            "histogram", "{chunk}", CHUNK_HISTOGRAM_BOUNDARIES
        ),
        INSTRUMENT_RETRIEVAL_REQUESTS: InstrumentSpec(
            "counter", "1", allowed_dimensions=("nkb.retrieval.outcome",)
        ),
        INSTRUMENT_RETRIEVAL_RESULTS: InstrumentSpec(
            "histogram", "{result}", RESULT_HISTOGRAM_BOUNDARIES
        ),
        INSTRUMENT_RETRIEVAL_DURATION: InstrumentSpec(
            "histogram", "s", NKB_DURATION_BOUNDARIES, ("nkb.retrieval.outcome",)
        ),
        INSTRUMENT_MODEL_COST: InstrumentSpec(
            "counter",
            "USD",
            (),
            (GEN_AI_OPERATION_NAME, NKB_MODEL_BILLING_PROVIDER, NKB_MODEL_BILLING_MODEL),
        ),
        INSTRUMENT_MODEL_COST_COMPONENTS: InstrumentSpec(
            "counter",
            "USD",
            (),
            (
                GEN_AI_OPERATION_NAME,
                NKB_MODEL_BILLING_PROVIDER,
                NKB_MODEL_BILLING_MODEL,
                NKB_MODEL_COST_COMPONENT,
            ),
        ),
        INSTRUMENT_MODEL_COST_ATTEMPTS: InstrumentSpec(
            "counter",
            "{attempt}",
            (),
            (
                GEN_AI_OPERATION_NAME,
                NKB_MODEL_BILLING_PROVIDER,
                NKB_MODEL_BILLING_MODEL,
                NKB_MODEL_COST_STATUS,
                NKB_MODEL_COST_REASON,
            ),
        ),
    }
)
