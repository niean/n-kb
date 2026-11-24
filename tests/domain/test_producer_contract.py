# tests/domain/test_producer_contract.py
"""跨服务契约单测：n-kb producer 真实消息输出对 n-agent 接收端校验规则的独立镜像。

接收端权威实现在 n-agent 仓库 app/interfaces/http/evaluation_routes.py（T17）。
本文件按其当前实际规则在测试内独立重写镜像校验函数，不 import n-agent 代码；
镜像漂移时本文件用例失败，提示人工同步两侧规则。

边界声明：
- 信封白名单、必需键、schema_version 一致性、results 身份键、五计数非负 int
  （拒绝 bool）等断言镜像的是接收端已有校验；
- observed_at 显式时区偏移与非有限 score 拒绝是 producer 侧更严格的约束，
  不声称接收端已有（接收端 observed_at 只要求 ISO 可解析，producer 消息
  通道的 json.loads 也不拒绝 NaN/Infinity 字面量）；captured_at 显式偏移
  为双侧一致规则，接收端同样显式校验（routes _validate_sample_message）。
- 用例数据一律来自真实构造路径（T1 三类 builder、EvaluationCapture 捕获链、
  T6 检索路由 evaluation_context 投影），不使用手写消息 JSON 作为正向用例。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.evaluation_capture import EvaluationCapture
from app.application.retrieval_service import (
    RetrievalContext,
    RetrievalOutcome,
    _response_projection,
)
from app.domain.evaluation import (
    SAMPLE_MESSAGE_TYPE,
    SERVICE_NAME,
    STATE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    EvaluationMessageError,
    PRODUCER_STATE_COUNT_KEYS,
    build_sample_message,
    build_state_message,
    build_target_deleted_message,
    canonical_json_bytes,
    compute_integrity,
)
from app.domain.retrieval import RetrievalFilter, RetrievalResult
from app.interfaces.http.routes import register_routes

# ---------------------------------------------------------------------------
# 接收端镜像（独立重写自 n-agent evaluation_routes.py 当前实际规则）
# ---------------------------------------------------------------------------

# 接收端 _SAMPLE_ALLOWED_KEYS 镜像（13 键封闭白名单）
_MIRROR_SAMPLE_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "producer_event_id",
    "service",
    "retrieval_id",
    "captured_at",
    "query",
    "top_k",
    "filters",
    "results",
    "corpus_revision",
    "trace_ref",
    "integrity",
})

# 接收端 _STATE_ALLOWED_KEYS 镜像（五计数键在接收端经 domain 常量展开，
# 此处按同一键表显式展开，键名与 n-kb PRODUCER_STATE_COUNT_KEYS 一致）
_MIRROR_STATE_COUNT_KEYS = (
    "eligible_count",
    "selected_count",
    "captured_count",
    "capture_error_count",
    "queue_full_count",
)
_MIRROR_STATE_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "service",
    "producer_id",
    "counter_epoch",
    "sequence",
    *_MIRROR_STATE_COUNT_KEYS,
    "observed_at",
})

# 接收端 _DELETED_ALLOWED_KEYS 镜像
_MIRROR_DELETED_ALLOWED_KEYS = frozenset({
    "type",
    "schema_version",
    "service",
    "document_id",
    "document_revision",
    "reason",
})

# 接收端 _RESULT_REQUIRED_KEYS 镜像（results 条目稳定身份必需键）
_MIRROR_RESULT_REQUIRED_KEYS = (
    "document_id",
    "document_revision",
    "chunk_id",
    "chunk_content_hash",
    "rank",
)

# 接收端 CLOSED_MESSAGE_TYPES 镜像
_MIRROR_CLOSED_MESSAGE_TYPES = (
    "evaluation.sample.v1",
    "evaluation.producer-state.v1",
    "evaluation.target-deleted.v1",
)


class MirrorProtocolError(Exception):
    """镜像校验失败（对应接收端 422 protocol_invalid 映射）。"""


def _mirror_require_present(msg: dict, key: str):
    if key not in msg:
        raise MirrorProtocolError(f"缺少必需字段: {key}")
    return msg[key]


def _mirror_require_non_empty_str(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MirrorProtocolError(f"{label} 必须是非空字符串")
    return value


def _mirror_require_non_negative_int(value, label: str) -> int:
    # 接收端 require_non_negative_int 镜像：bool 不是整数
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MirrorProtocolError(f"{label} 必须是非负整数")
    return value


def _mirror_check_closed_envelope(msg: dict) -> str:
    msg_type = _mirror_require_present(msg, "type")
    if msg_type not in _MIRROR_CLOSED_MESSAGE_TYPES:
        raise MirrorProtocolError("未知消息 type（封闭枚举）")
    schema_version = _mirror_require_present(msg, "schema_version")
    if schema_version != msg_type:
        raise MirrorProtocolError("schema_version 必须与 type 一致")
    return msg_type


def mirror_validate_sample_message(msg: dict) -> None:
    """接收端 _validate_sample_message 当前实际规则的独立镜像。"""
    for key in msg:
        if key not in _MIRROR_SAMPLE_ALLOWED_KEYS:
            raise MirrorProtocolError("消息包含未知顶层字段（封闭白名单）")
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "producer_event_id"), "producer_event_id"
    )
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "retrieval_id"), "retrieval_id"
    )
    captured_at = _mirror_require_non_empty_str(
        _mirror_require_present(msg, "captured_at"), "captured_at"
    )
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError:
        raise MirrorProtocolError("captured_at 必须是 ISO-8601 时间戳")
    if captured.tzinfo is None:
        # 接收端要求 captured_at 携带时区偏移（naive 拒绝）
        raise MirrorProtocolError("captured_at 必须携带时区偏移（UTC）")
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "corpus_revision"), "corpus_revision"
    )
    if not isinstance(msg.get("query"), str):
        raise MirrorProtocolError("query 必须是字符串")
    results = _mirror_require_present(msg, "results")
    if not isinstance(results, list):
        raise MirrorProtocolError("results 必须是数组")
    for entry in results:
        if not isinstance(entry, dict):
            raise MirrorProtocolError("results 条目必须是对象")
        for key in _MIRROR_RESULT_REQUIRED_KEYS:
            value = _mirror_require_present(entry, key)
            if key == "rank":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise MirrorProtocolError("results.rank 必须是正整数")
            else:
                _mirror_require_non_empty_str(value, f"results.{key}")
        snippet = entry.get("snippet")
        if snippet is not None and not isinstance(snippet, str):
            raise MirrorProtocolError("results.snippet 必须是字符串")
        score = entry.get("similarity_score")
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float))
        ):
            raise MirrorProtocolError("results.similarity_score 必须是数值")
    top_k = msg.get("top_k")
    if top_k is not None and (
        isinstance(top_k, bool) or not isinstance(top_k, int)
    ):
        raise MirrorProtocolError("top_k 必须是整数")
    filters = msg.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise MirrorProtocolError("filters 必须是对象")
    trace_ref = _mirror_require_present(msg, "trace_ref")
    if trace_ref is not None and not isinstance(trace_ref, dict):
        raise MirrorProtocolError("trace_ref 必须是对象或 null")
    integrity = _mirror_require_present(msg, "integrity")
    if not isinstance(integrity, dict):
        raise MirrorProtocolError("integrity 必须是对象")
    _mirror_require_non_empty_str(
        _mirror_require_present(integrity, "redaction_version"),
        "integrity.redaction_version",
    )


def mirror_validate_state_message(msg: dict) -> None:
    """接收端 _validate_state_message 当前实际规则的独立镜像。

    注意：接收端对 observed_at 只要求 ISO-8601 可解析，不要求时区偏移；
    显式偏移是 producer 侧更严格的约束，不得写进镜像。"""
    for key in msg:
        if key not in _MIRROR_STATE_ALLOWED_KEYS:
            raise MirrorProtocolError("消息包含未知顶层字段（封闭白名单）")
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "producer_id"), "producer_id"
    )
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "counter_epoch"), "counter_epoch"
    )
    _mirror_require_non_negative_int(
        _mirror_require_present(msg, "sequence"), "sequence"
    )
    for key in _MIRROR_STATE_COUNT_KEYS:
        _mirror_require_non_negative_int(_mirror_require_present(msg, key), key)
    observed_at = _mirror_require_non_empty_str(
        _mirror_require_present(msg, "observed_at"), "observed_at"
    )
    try:
        datetime.fromisoformat(observed_at)
    except ValueError:
        raise MirrorProtocolError("observed_at 必须是 ISO-8601 时间戳")


def mirror_validate_deleted_message(msg: dict) -> None:
    """接收端 _validate_deleted_message 当前实际规则的独立镜像。"""
    for key in msg:
        if key not in _MIRROR_DELETED_ALLOWED_KEYS:
            raise MirrorProtocolError("消息包含未知顶层字段（封闭白名单）")
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "document_id"), "document_id"
    )
    _mirror_require_non_empty_str(
        _mirror_require_present(msg, "document_revision"), "document_revision"
    )
    reason = msg.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise MirrorProtocolError("reason 必须是字符串")


# ---------------------------------------------------------------------------
# 镜像负例（每条 raise 路径至少一个负例；检测镜像过松方向的规则笔误）
# ---------------------------------------------------------------------------


def _pop(msg: dict, key: str) -> dict:
    msg.pop(key)
    return msg


def _put(msg: dict, key: str, value) -> dict:
    msg[key] = value
    return msg


def _put_entry(msg: dict, key: str, value) -> dict:
    msg["results"][0][key] = value
    return msg


def _pop_entry(msg: dict, key: str) -> dict:
    msg["results"][0].pop(key)
    return msg


def _valid_sample_literal() -> dict:
    # 负例基线允许手工构造（plan 允许负例 literal dict）；形状对齐真实输出
    return {
        "type": "evaluation.sample.v1",
        "schema_version": "evaluation.sample.v1",
        "producer_event_id": "evt-neg",
        "service": "n-kb",
        "retrieval_id": "r-neg",
        "captured_at": "2026-11-24T08:00:00+00:00",
        "query": "q",
        "top_k": 5,
        "filters": {},
        "results": [
            {
                "document_id": "d",
                "document_revision": "rev",
                "chunk_id": "c",
                "chunk_content_hash": "h",
                "rank": 1,
            }
        ],
        "corpus_revision": "rev",
        "trace_ref": None,
        "integrity": {"redaction_version": "v0"},
    }


def _valid_state_literal() -> dict:
    return {
        "type": "evaluation.producer-state.v1",
        "schema_version": "evaluation.producer-state.v1",
        "service": "n-kb",
        "producer_id": "n-kb",
        "counter_epoch": "2026-11-24T08:00:00+00:00",
        "sequence": 1,
        "eligible_count": 0,
        "selected_count": 0,
        "captured_count": 0,
        "capture_error_count": 0,
        "queue_full_count": 0,
        "observed_at": "2026-11-24T08:00:05+00:00",
    }


def _valid_deleted_literal() -> dict:
    return {
        "type": "evaluation.target-deleted.v1",
        "schema_version": "evaluation.target-deleted.v1",
        "service": "n-kb",
        "document_id": "d",
        "document_revision": "rev",
        "reason": "document_deleted",
    }


_SAMPLE_MIRROR_NEGATIVES = [
    pytest.param(lambda m: _put(m, "unknown_key", 1), id="unknown_top_level_key"),
    pytest.param(lambda m: _pop(m, "producer_event_id"), id="missing_producer_event_id"),
    pytest.param(lambda m: _put(m, "producer_event_id", ""), id="empty_producer_event_id"),
    pytest.param(lambda m: _pop(m, "retrieval_id"), id="missing_retrieval_id"),
    pytest.param(lambda m: _put(m, "retrieval_id", ""), id="empty_retrieval_id"),
    pytest.param(lambda m: _pop(m, "captured_at"), id="missing_captured_at"),
    pytest.param(lambda m: _put(m, "captured_at", ""), id="empty_captured_at"),
    pytest.param(lambda m: _put(m, "captured_at", "not-a-time"), id="malformed_captured_at"),
    pytest.param(lambda m: _put(m, "captured_at", "2026-11-24T08:00:00"), id="naive_captured_at"),
    pytest.param(lambda m: _pop(m, "corpus_revision"), id="missing_corpus_revision"),
    pytest.param(lambda m: _put(m, "corpus_revision", ""), id="empty_corpus_revision"),
    pytest.param(lambda m: _put(m, "query", 123), id="query_not_string"),
    pytest.param(lambda m: _put(m, "results", "no"), id="results_not_list"),
    pytest.param(lambda m: _put(m, "results", ["entry"]), id="result_entry_not_dict"),
]
for _key in ("document_id", "document_revision", "chunk_id", "chunk_content_hash"):
    _SAMPLE_MIRROR_NEGATIVES.append(
        pytest.param(lambda m, k=_key: _pop_entry(m, k), id=f"result_missing_{_key}")
    )
_SAMPLE_MIRROR_NEGATIVES += [
    pytest.param(lambda m: _put_entry(m, "rank", 0), id="rank_zero"),
    pytest.param(lambda m: _put_entry(m, "rank", True), id="rank_bool"),
    pytest.param(lambda m: _put_entry(m, "rank", "1"), id="rank_string"),
    pytest.param(lambda m: _put_entry(m, "rank", 1.5), id="rank_float"),
    pytest.param(lambda m: _put_entry(m, "document_id", ""), id="result_identity_empty"),
    pytest.param(lambda m: _put_entry(m, "document_id", 42), id="result_identity_not_string"),
    pytest.param(lambda m: _put_entry(m, "snippet", 5), id="snippet_not_string"),
    pytest.param(lambda m: _put_entry(m, "similarity_score", "x"), id="score_not_numeric"),
    pytest.param(lambda m: _put_entry(m, "similarity_score", True), id="score_bool"),
    pytest.param(lambda m: _put(m, "top_k", True), id="top_k_bool"),
    pytest.param(lambda m: _put(m, "top_k", "5"), id="top_k_not_int"),
    pytest.param(lambda m: _put(m, "filters", []), id="filters_not_dict"),
    pytest.param(lambda m: _pop(m, "trace_ref"), id="missing_trace_ref"),
    pytest.param(lambda m: _put(m, "trace_ref", "t"), id="trace_ref_not_dict"),
    pytest.param(lambda m: _pop(m, "integrity"), id="missing_integrity"),
    pytest.param(lambda m: _put(m, "integrity", []), id="integrity_not_dict"),
    pytest.param(lambda m: _put(m, "integrity", {"other": "x"}), id="integrity_missing_redaction_version"),
    pytest.param(lambda m: _put(m, "integrity", {"redaction_version": ""}), id="empty_redaction_version"),
]

_STATE_MIRROR_NEGATIVES = [
    pytest.param(lambda m: _put(m, "unknown_key", 1), id="unknown_top_level_key"),
    pytest.param(lambda m: _pop(m, "producer_id"), id="missing_producer_id"),
    pytest.param(lambda m: _put(m, "producer_id", ""), id="empty_producer_id"),
    pytest.param(lambda m: _pop(m, "counter_epoch"), id="missing_counter_epoch"),
    pytest.param(lambda m: _put(m, "counter_epoch", ""), id="empty_counter_epoch"),
    pytest.param(lambda m: _pop(m, "sequence"), id="missing_sequence"),
    pytest.param(lambda m: _put(m, "sequence", True), id="sequence_bool"),
    pytest.param(lambda m: _put(m, "sequence", -1), id="sequence_negative"),
    pytest.param(lambda m: _put(m, "sequence", "1"), id="sequence_not_int"),
]
for _key in _MIRROR_STATE_COUNT_KEYS:
    _STATE_MIRROR_NEGATIVES.append(
        pytest.param(lambda m, k=_key: _pop(m, k), id=f"missing_count_{_key}")
    )
_STATE_MIRROR_NEGATIVES += [
    pytest.param(lambda m: _put(m, "captured_count", True), id="count_bool"),
    pytest.param(lambda m: _put(m, "captured_count", -1), id="count_negative"),
    pytest.param(lambda m: _put(m, "captured_count", "1"), id="count_not_int"),
    pytest.param(lambda m: _pop(m, "observed_at"), id="missing_observed_at"),
    pytest.param(lambda m: _put(m, "observed_at", ""), id="empty_observed_at"),
    pytest.param(lambda m: _put(m, "observed_at", "not-a-time"), id="malformed_observed_at"),
]

_DELETED_MIRROR_NEGATIVES = [
    pytest.param(lambda m: _put(m, "unknown_key", 1), id="unknown_top_level_key"),
    pytest.param(lambda m: _pop(m, "document_id"), id="missing_document_id"),
    pytest.param(lambda m: _put(m, "document_id", ""), id="empty_document_id"),
    pytest.param(lambda m: _pop(m, "document_revision"), id="missing_document_revision"),
    pytest.param(lambda m: _put(m, "document_revision", ""), id="empty_document_revision"),
    pytest.param(lambda m: _put(m, "reason", 5), id="reason_not_string"),
]

_ENVELOPE_NEGATIVES = [
    pytest.param(lambda m: _pop(m, "type"), id="missing_type"),
    pytest.param(lambda m: _put(m, "type", "evaluation.other.v1"), id="type_not_in_closed_set"),
    pytest.param(lambda m: _pop(m, "schema_version"), id="missing_schema_version"),
    pytest.param(lambda m: _put(m, "schema_version", "v9"), id="schema_version_mismatch"),
]


class TestMirrorNegatives:
    """镜像校验函数的负例覆盖：任何规则被写松（笔误）都会在此失败。"""

    @pytest.mark.parametrize("mutate", _SAMPLE_MIRROR_NEGATIVES)
    def test_sample_mirror_rejects_negative(self, mutate):
        with pytest.raises(MirrorProtocolError):
            mirror_validate_sample_message(mutate(_valid_sample_literal()))

    @pytest.mark.parametrize("mutate", _STATE_MIRROR_NEGATIVES)
    def test_state_mirror_rejects_negative(self, mutate):
        with pytest.raises(MirrorProtocolError):
            mirror_validate_state_message(mutate(_valid_state_literal()))

    @pytest.mark.parametrize("mutate", _DELETED_MIRROR_NEGATIVES)
    def test_deleted_mirror_rejects_negative(self, mutate):
        with pytest.raises(MirrorProtocolError):
            mirror_validate_deleted_message(mutate(_valid_deleted_literal()))

    @pytest.mark.parametrize("mutate", _ENVELOPE_NEGATIVES)
    def test_envelope_rejects_negative(self, mutate):
        with pytest.raises(MirrorProtocolError):
            _mirror_check_closed_envelope(mutate(_valid_sample_literal()))


# ---------------------------------------------------------------------------
# 真实构造路径
# ---------------------------------------------------------------------------


class _OutboxStoreStub:
    """捕获链出箱桩：记录 EvaluationCapture 真实登记的样本消息。"""

    def __init__(self) -> None:
        self.registered = None

    def register_sample_with_counter(self, message, delta, max_rows, max_bytes):
        self.registered = message

    def increment_counters(self, delta):
        pass


def _real_retrieval_results() -> list[RetrievalResult]:
    """与 T6 检索接口测试同形状的真实结果（身份键齐全、携带 vector 字段）。"""
    return [
        RetrievalResult(
            document_id="doc-1",
            chunk_id="chunk-1",
            score=0.87,
            snippet="safe snippet",
            source={"kind": "upload", "uri": "note.md"},
            tags={"topic": "rag"},
            metadata={"ordinal": 0, "vector": [1.0, 2.0]},
            document_revision="dochash-1",
            chunk_content_hash="chunkhash-1",
        ),
        RetrievalResult(
            document_id="doc-2",
            chunk_id="chunk-2",
            score=0.62,
            snippet="second snippet",
            source={"kind": "upload", "uri": "note-2.md"},
            tags={"topic": "kb"},
            metadata={"ordinal": 1},
            document_revision="dochash-2",
            chunk_content_hash="chunkhash-2",
        ),
    ]


def _capture_sample_via_real_path(results, query="what is rag", top_k=5):
    """经 EvaluationCapture 真实捕获链投影 results 并返回登记的样本消息。

    context 与 RetrievalService._build_context 同形状：显式 UTC 偏移的
    captured_at、占位 integrity 由成功路径回填语义保持一致。"""
    store = _OutboxStoreStub()
    capture = EvaluationCapture(store, enabled=True)
    context = RetrievalContext(
        retrieval_id="retrieval-contract-1",
        captured_at="2026-11-24T08:00:00+00:00",
        corpus_revision="rev-contract-1",
        trace_ref={"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True},
        integrity=compute_integrity(b"", b"", truncated=False, incomplete=True),
    )
    filters = RetrievalFilter(tags={"topic": "rag"}, min_score=0.5)
    capture.capture_success(context, query, filters, top_k, results)
    return store.registered


def _assert_envelope_contract(msg: dict, message_type: str, allowed_keys) -> None:
    """三类消息共用的信封契约断言（镜像接收端封闭枚举与白名单规则）。"""
    assert _mirror_check_closed_envelope(msg) == message_type
    assert msg["type"] == msg["schema_version"] == message_type
    assert set(msg) <= allowed_keys
    assert msg["service"] == SERVICE_NAME


# ---------------------------------------------------------------------------
# sample 消息契约（真实捕获链输出）
# ---------------------------------------------------------------------------


class TestSampleMessageContract:
    def test_real_capture_output_passes_receiver_mirror(self):
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        assert msg is not None
        mirror_validate_sample_message(msg)

    def test_sample_envelope_keys_within_receiver_whitelist(self):
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        _assert_envelope_contract(msg, SAMPLE_MESSAGE_TYPE, _MIRROR_SAMPLE_ALLOWED_KEYS)
        # 接收端必需键全部存在
        for key in (
            "producer_event_id",
            "retrieval_id",
            "captured_at",
            "corpus_revision",
            "query",
            "results",
            "trace_ref",
            "integrity",
        ):
            assert key in msg
        assert msg["producer_event_id"]
        assert msg["retrieval_id"]
        assert msg["corpus_revision"]
        assert isinstance(msg["query"], str)
        assert isinstance(msg["results"], list)

    def test_sample_results_identity_keys_and_rank(self):
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        for entry in msg["results"]:
            for key in _MIRROR_RESULT_REQUIRED_KEYS:
                assert key in entry
                if key == "rank":
                    assert not isinstance(entry[key], bool)
                    assert isinstance(entry[key], int) and entry[key] >= 1
                else:
                    assert isinstance(entry[key], str) and entry[key]
        # rank 保留原始位次且递增
        assert [entry["rank"] for entry in msg["results"]] == [1, 2]

    def test_sample_captured_at_has_explicit_offset(self):
        # producer 侧约束（接收端同样要求，此处锚定真实输出不回退）
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        parsed = datetime.fromisoformat(msg["captured_at"])
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None

    def test_sample_trace_ref_and_integrity_shapes(self):
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        assert isinstance(msg["trace_ref"], dict)
        assert isinstance(msg["integrity"], dict)
        assert isinstance(msg["integrity"].get("redaction_version"), str)
        assert msg["integrity"]["redaction_version"]

    def test_producer_rejects_naive_captured_at(self):
        # negative 用例基于真实输出派生，仍走真实 builder 校验
        results = _real_retrieval_results()
        with pytest.raises(EvaluationMessageError):
            build_sample_message(
                producer_event_id="evt-contract-1",
                retrieval_id="retrieval-contract-1",
                captured_at="2026-11-24T08:00:00",
                query="q",
                top_k=5,
                filters={},
                results=[
                    {
                        "document_id": r.document_id,
                        "document_revision": r.document_revision,
                        "chunk_id": r.chunk_id,
                        "chunk_content_hash": r.chunk_content_hash,
                        "rank": rank,
                    }
                    for rank, r in enumerate(results, start=1)
                ],
                corpus_revision="rev-contract-1",
                trace_ref=None,
                integrity=compute_integrity(b"", b"", truncated=False, incomplete=False),
            )


# ---------------------------------------------------------------------------
# producer-state 消息契约（真实 builder 输出，计数为真实零基线形状）
# ---------------------------------------------------------------------------


class TestStateMessageContract:
    def _real_state_message(self) -> dict:
        # 与 sqlite_store.record_producer_state 相同的零基线计数形状
        counts = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}
        return build_state_message(
            counter_epoch="2026-11-24T08:00:00+00:00",
            sequence=1,
            counts=counts,
            observed_at="2026-11-24T08:00:05+00:00",
        )

    def test_real_state_output_passes_receiver_mirror(self):
        mirror_validate_state_message(self._real_state_message())

    def test_state_envelope_keys_within_receiver_whitelist(self):
        msg = self._real_state_message()
        _assert_envelope_contract(msg, STATE_MESSAGE_TYPE, _MIRROR_STATE_ALLOWED_KEYS)
        for key in (
            "producer_id",
            "counter_epoch",
            "sequence",
            "observed_at",
            *_MIRROR_STATE_COUNT_KEYS,
        ):
            assert key in msg
        assert msg["producer_id"] == SERVICE_NAME

    def test_state_five_counters_non_negative_int_not_bool(self):
        msg = self._real_state_message()
        for key in _MIRROR_STATE_COUNT_KEYS:
            value = msg[key]
            assert not isinstance(value, bool)
            assert isinstance(value, int) and value >= 0

    def test_producer_rejects_naive_observed_at(self):
        # producer 更严格：接收端镜像只要求 ISO 可解析（naive 可通过镜像），
        # producer builder 拒绝 naive——该差异是 producer 侧约束，不归属接收端
        counts = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="2026-11-24T08:00:00+00:00",
                sequence=1,
                counts=counts,
                observed_at="2026-11-24T08:00:05",
            )
        naive_msg = self._real_state_message()
        naive_msg["observed_at"] = "2026-11-24T08:00:05"
        mirror_validate_state_message(naive_msg)  # 镜像（接收端）不拒绝


# ---------------------------------------------------------------------------
# target-deleted 消息契约（真实 builder 输出，revision 为删除前 content_hash 语义）
# ---------------------------------------------------------------------------


class TestDeletedMessageContract:
    def _real_deleted_message(self) -> dict:
        return build_target_deleted_message(
            document_id="doc-contract-1",
            document_revision="dochash-1",
            reason="document_deleted",
        )

    def test_real_deleted_output_passes_receiver_mirror(self):
        mirror_validate_deleted_message(self._real_deleted_message())

    def test_deleted_envelope_keys_within_receiver_whitelist(self):
        msg = self._real_deleted_message()
        _assert_envelope_contract(msg, TARGET_DELETED_MESSAGE_TYPE, _MIRROR_DELETED_ALLOWED_KEYS)
        assert isinstance(msg["document_id"], str) and msg["document_id"]
        assert isinstance(msg["document_revision"], str) and msg["document_revision"]
        assert isinstance(msg["reason"], str)

    def test_deleted_reason_optional_for_receiver(self):
        # reason 缺省时接收端镜像放行（reason 非字符串才拒绝）；producer
        # 落库路径恒传 reason 字符串
        msg = self._real_deleted_message()
        del msg["reason"]
        mirror_validate_deleted_message(msg)


# ---------------------------------------------------------------------------
# 非有限 score 拒绝（producer 侧约束：canonical 编码 allow_nan=False）
# ---------------------------------------------------------------------------


class TestNonFiniteScoreRejection:
    def test_real_capture_skips_non_finite_score(self):
        # 捕获链先经 canonical 编码投影，NaN 触发 EvaluationMessageError，
        # 样本整体跳过、不入箱
        results = _real_retrieval_results()
        nan_result = RetrievalResult(
            document_id="doc-nan",
            chunk_id="chunk-nan",
            score=float("nan"),
            snippet="nan snippet",
            source={"kind": "upload", "uri": "note.md"},
            tags={},
            metadata={},
            document_revision="dochash-nan",
            chunk_content_hash="chunkhash-nan",
        )
        store = _OutboxStoreStub()
        capture = EvaluationCapture(store, enabled=True)
        context = RetrievalContext(
            retrieval_id="retrieval-contract-2",
            captured_at="2026-11-24T08:00:00+00:00",
            corpus_revision="rev-contract-2",
            trace_ref=None,
            integrity=compute_integrity(b"", b"", truncated=False, incomplete=True),
        )
        capture.capture_success(context, "q", RetrievalFilter(), 5, [*results, nan_result])
        assert store.registered is None

    def test_canonical_encoding_rejects_non_finite_in_real_message(self):
        # 真实样本消息混入非有限 score 后 canonical 编码必须失败；
        # 接收端 producer 通道的 json.loads 不拒绝该字面量，此为 producer
        # 侧更严格的出站保证，不声称接收端已有
        msg = _capture_sample_via_real_path(_real_retrieval_results())
        msg["results"][0]["similarity_score"] = float("inf")
        with pytest.raises(EvaluationMessageError):
            canonical_json_bytes(msg)


# ---------------------------------------------------------------------------
# T6 真实 HTTP fixture：evaluation_context 响应形状与 adapter 消费契约
# ---------------------------------------------------------------------------


class _FixtureRetrievalService:
    """与 tests/interfaces/test_retrieval_api.py T6 fixture 同构的最小检索服务。"""

    def search_with_context(self, query, filters=None, top_k=5):
        results = _real_retrieval_results()
        original = canonical_json_bytes(_response_projection(results))
        context = RetrievalContext(
            retrieval_id="retrieval-contract-3",
            captured_at="2026-11-24T08:00:00+00:00",
            corpus_revision="rev-contract-3",
            trace_ref={"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True},
            integrity=compute_integrity(original, original, truncated=False, incomplete=False),
        )
        return RetrievalOutcome(results=results, context=context)


class TestEvaluationContextHttpContract:
    def _post_search(self):
        app = FastAPI()
        app.state.services = {"retrieval": _FixtureRetrievalService()}
        register_routes(app)
        client = TestClient(app)
        return client.post("/retrieval/search", json={"query": "what is rag"})

    def test_context_shape_matches_adapter_consumed_keys(self):
        # n-agent http_adapters._parse_evaluation_context 消费 retrieval_id/
        # corpus_revision/trace_ref/integrity；captured_at 是响应契约字段
        response = self._post_search()
        assert response.status_code == 200
        context = response.json()["evaluation_context"]
        assert set(context) == {
            "retrieval_id",
            "captured_at",
            "corpus_revision",
            "trace_ref",
            "integrity",
        }
        assert isinstance(context["retrieval_id"], str) and context["retrieval_id"]
        assert isinstance(context["corpus_revision"], str) and context["corpus_revision"]
        assert isinstance(context["trace_ref"], dict)
        assert isinstance(context["integrity"], dict)
        parsed = datetime.fromisoformat(context["captured_at"])
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None

    def test_context_integrity_keys_match_compute_integrity(self):
        response = self._post_search()
        integrity = response.json()["evaluation_context"]["integrity"]
        assert set(integrity) == {
            "original_bytes",
            "truncated",
            "incomplete",
            "observed_bytes",
            "redaction_version",
        }
        assert integrity["truncated"] is False
        assert integrity["incomplete"] is False
        assert integrity["original_bytes"] == integrity["observed_bytes"]

    def test_response_results_carry_identity_keys_for_sample_contract(self):
        # results 条目必须携带接收端 results 身份契约所需的键，producer
        # 投影才能构造出可过接收端校验的样本
        response = self._post_search()
        payload = response.json()
        for entry in payload["results"]:
            for key in ("document_id", "document_revision", "chunk_id", "chunk_content_hash"):
                assert key in entry
                assert isinstance(entry[key], str) and entry[key]
        # 响应不得泄漏 vector 字段
        assert "vector" not in payload["results"][0]["metadata"]

    def test_fixture_context_feeds_builder_output_passing_receiver_mirror(self):
        # 闭环：HTTP fixture context/results 喂给真实 builder 后的样本消息
        # 必须能通过接收端镜像校验
        response = self._post_search()
        payload = response.json()
        context = payload["evaluation_context"]
        results = [
            {
                "document_id": entry["document_id"],
                "document_revision": entry["document_revision"],
                "chunk_id": entry["chunk_id"],
                "chunk_content_hash": entry["chunk_content_hash"],
                "rank": rank,
                "similarity_score": entry["score"],
                "snippet": entry["snippet"],
            }
            for rank, entry in enumerate(payload["results"], start=1)
        ]
        msg = build_sample_message(
            producer_event_id="evt-contract-http",
            retrieval_id=context["retrieval_id"],
            captured_at=context["captured_at"],
            query=payload["query"],
            top_k=5,
            filters={},
            results=results,
            corpus_revision=context["corpus_revision"],
            trace_ref=context["trace_ref"],
            integrity=context["integrity"],
        )
        mirror_validate_sample_message(msg)
        # canonical 字节级确定性：接收端按字节复核幂等 hash 的前提
        assert json.loads(canonical_json_bytes(msg).decode("utf-8")) == msg
