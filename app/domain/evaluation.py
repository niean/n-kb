# app/domain/evaluation.py
"""评估采集消息构造与 canonical JSON 编码。

本模块是评估采集链路的契约基础：三类消息（sample 采样、producer-state 心跳、
target-deleted 墓碑）由纯函数构造，键集为严格白名单，接收方按同一白名单校验并
拒绝未知键。全部消息必须经 canonical_json_bytes 序列化：键按字典序、紧凑分隔符、
UTF-8、拒绝非有限数，保证字节级确定性。零 IO、零框架依赖。

时间戳一律要求携带时区偏移的 ISO 8601 字符串（naive 时间戳拒绝）；计数器键名与
n-agent 生产者侧保持一致，但在本模块独立声明。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

# 消息类型与 schema 版本同源；字段语义变化时递增版本
SAMPLE_MESSAGE_TYPE = "evaluation.sample.v1"
STATE_MESSAGE_TYPE = "evaluation.producer-state.v1"
TARGET_DELETED_MESSAGE_TYPE = "evaluation.target-deleted.v1"

SERVICE_NAME = "n-kb"

# producer-state 五计数键名（与 n-agent 生产者侧一致，独立声明）
PRODUCER_STATE_COUNT_KEYS: tuple[str, ...] = (
    "eligible_count",
    "selected_count",
    "captured_count",
    "capture_error_count",
    "queue_full_count",
)

# sample.results 逐条必需的定位键
_RESULT_REQUIRED_KEYS: tuple[str, ...] = (
    "document_id",
    "document_revision",
    "chunk_id",
    "chunk_content_hash",
    "rank",
)


class EvaluationMessageError(ValueError):
    """评估消息构造或 canonical 编码失败。"""


class OutboxCapacityError(RuntimeError):
    """评估 outbox 容量超限（行数或字节任一上限将被超出；恰好等于上限允许）。"""


def _reject_bool(value: Any) -> None:
    if isinstance(value, bool):
        raise EvaluationMessageError("bool 不允许作为数值字段")


def _require_tz_aware_iso(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise EvaluationMessageError(f"{field_name} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationMessageError(f"{field_name} 不是合法 ISO 8601 时间戳") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationMessageError(f"{field_name} 必须携带时区偏移（拒绝 naive 时间戳）")


def canonical_json_bytes(content: Mapping) -> bytes:
    """确定性 JSON 编码：键字典序、紧凑分隔符、UTF-8、拒绝 NaN/Inf。"""
    if not isinstance(content, Mapping):
        raise EvaluationMessageError("canonical_json_bytes 仅接受 Mapping 输入")
    try:
        text = json.dumps(
            content,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationMessageError(f"内容无法确定性序列化: {exc}") from exc
    return text.encode("utf-8")


def compute_integrity(
    original: bytes, observed: bytes, *, truncated: bool, incomplete: bool
) -> dict:
    """构造采样负载的完整性事实。"""
    return {
        "original_bytes": len(original),
        "truncated": truncated,
        "incomplete": incomplete,
        "observed_bytes": len(observed),
        "redaction_version": "v0",
    }


def _validate_results(results: Any) -> None:
    if not isinstance(results, list):
        raise EvaluationMessageError("results 必须是列表")
    for index, result in enumerate(results):
        if not isinstance(result, Mapping):
            raise EvaluationMessageError(f"results[{index}] 必须是映射")
        missing = [key for key in _RESULT_REQUIRED_KEYS if key not in result]
        if missing:
            raise EvaluationMessageError(
                f"results[{index}] 缺少必需键: {', '.join(missing)}"
            )


def build_sample_message(
    *,
    producer_event_id: str,
    retrieval_id: str,
    captured_at: str,
    query: str,
    top_k: int,
    filters: Mapping,
    results: list,
    corpus_revision: str,
    trace_ref: Any,
    integrity: Mapping,
) -> dict:
    """构造 evaluation.sample.v1 采样消息（严格白名单键集）。

    filters 与 integrity 入参拷贝为浅拷贝、results 为浅列表拷贝：嵌套 dict
    与调用方共享，构造完成后调用方不得原地修改，否则可能破坏已产出消息的
    canonical 序列化结果。
    """
    _reject_bool(top_k)
    if not isinstance(top_k, int) or top_k < 0:
        raise EvaluationMessageError("top_k 必须是非负 int")
    _require_tz_aware_iso(captured_at, "captured_at")
    _validate_results(results)
    if not isinstance(filters, Mapping):
        raise EvaluationMessageError("filters 必须是映射")
    if not isinstance(integrity, Mapping):
        raise EvaluationMessageError("integrity 必须是映射")
    return {
        "type": SAMPLE_MESSAGE_TYPE,
        "schema_version": SAMPLE_MESSAGE_TYPE,
        "producer_event_id": producer_event_id,
        "service": SERVICE_NAME,
        "retrieval_id": retrieval_id,
        "captured_at": captured_at,
        "query": query,
        "top_k": top_k,
        "filters": dict(filters),
        "results": list(results),
        "corpus_revision": corpus_revision,
        "trace_ref": trace_ref,
        "integrity": dict(integrity),
    }


def build_state_message(
    *,
    counter_epoch: str,
    sequence: int,
    counts: Mapping[str, int],
    observed_at: str,
) -> dict:
    """构造 evaluation.producer-state.v1 心跳消息（五计数非负 int、拒绝 bool）。"""
    _reject_bool(sequence)
    if not isinstance(sequence, int) or sequence < 0:
        raise EvaluationMessageError("sequence 必须是非负 int")
    missing = [key for key in PRODUCER_STATE_COUNT_KEYS if key not in counts]
    if missing:
        raise EvaluationMessageError(f"counts 缺少计数键: {', '.join(missing)}")
    validated_counts: dict[str, int] = {}
    for key in PRODUCER_STATE_COUNT_KEYS:
        value = counts[key]
        _reject_bool(value)
        if not isinstance(value, int) or value < 0:
            raise EvaluationMessageError(f"counts[{key}] 必须是非负 int")
        validated_counts[key] = value
    _require_tz_aware_iso(observed_at, "observed_at")
    return {
        "type": STATE_MESSAGE_TYPE,
        "schema_version": STATE_MESSAGE_TYPE,
        "service": SERVICE_NAME,
        "producer_id": SERVICE_NAME,
        "counter_epoch": counter_epoch,
        "sequence": sequence,
        **validated_counts,
        "observed_at": observed_at,
    }


def build_target_deleted_message(
    *, document_id: str, document_revision: str, reason: str
) -> dict:
    """构造 evaluation.target-deleted.v1 墓碑消息。"""
    return {
        "type": TARGET_DELETED_MESSAGE_TYPE,
        "schema_version": TARGET_DELETED_MESSAGE_TYPE,
        "service": SERVICE_NAME,
        "document_id": document_id,
        "document_revision": document_revision,
        "reason": reason,
    }
