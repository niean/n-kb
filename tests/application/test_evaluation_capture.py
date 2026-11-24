"""EvaluationCapture 应用层测试：样本投影、信封预算、身份完整性与计数 delta 契约。

FakeEvaluationStore 精确模拟 T2 sqlite_store.register_sample_with_counter 契约：
成功路径按调用方 delta 原子累计；容量拒绝在拒绝事务内部提交 eligible/selected/
queue_full（按 delta 的 eligible_count）后抛 OutboxCapacityError；其他异常视为
事务回滚（计数未提交）。
"""
import sqlite3
import uuid

import pytest

from app.application.evaluation_capture import (
    ENVELOPE_MAX_BYTES,
    EvaluationCapture,
    OUTBOX_MAX_BYTES,
    OUTBOX_MAX_ROWS,
    RESULTS_MAX_BYTES,
    SAMPLE_MAX_RESULTS,
    SNIPPET_MAX_BYTES,
)
from app.application.retrieval_service import RetrievalContext, RetrievalService
from app.domain.evaluation import (
    PRODUCER_STATE_COUNT_KEYS,
    OutboxCapacityError,
    canonical_json_bytes,
)
from app.domain.retrieval import RetrievalFilter, RetrievalResult

SAMPLE_ENVELOPE_KEYS = {
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
}

RESULT_ENTRY_KEYS = {
    "document_id",
    "document_revision",
    "chunk_id",
    "chunk_content_hash",
    "rank",
    "similarity_score",
    "snippet",
}

CAPTURED_DELTA = {"eligible_count": 1, "selected_count": 1, "captured_count": 1}
CAPTURE_ERROR_DELTA = {"eligible_count": 1, "selected_count": 1, "capture_error_count": 1}


# ---------------------------------------------------------------------------
# fake store 与构造辅助
# ---------------------------------------------------------------------------


class FakeEvaluationStore:
    """按 T2 契约模拟 register_sample_with_counter / increment_counters。"""

    def __init__(self):
        self.register_calls = []
        self.increment_calls = []
        self.counters = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}
        self.reject_with_capacity = False
        self.fail_register = False
        self.fail_increment = False

    def register_sample_with_counter(self, payload, counter_delta, max_rows, max_bytes):
        self.register_calls.append(
            {
                "payload": payload,
                "delta": dict(counter_delta),
                "max_rows": max_rows,
                "max_bytes": max_bytes,
            }
        )
        if self.reject_with_capacity:
            # T2 契约：拒绝事务内部按 eligible_count 提交 eligible/selected/queue_full
            eligible = counter_delta["eligible_count"]
            self.counters["eligible_count"] += eligible
            self.counters["selected_count"] += eligible
            self.counters["queue_full_count"] += eligible
            raise OutboxCapacityError("evaluation outbox 容量超限")
        if self.fail_register:
            raise sqlite3.OperationalError("database is locked")
        for key, value in counter_delta.items():
            self.counters[key] += value
        return dict(self.counters)

    def increment_counters(self, delta):
        self.increment_calls.append(dict(delta))
        if self.fail_increment:
            raise sqlite3.OperationalError("database is locked")
        for key, value in delta.items():
            self.counters[key] += value
        return dict(self.counters)


def make_context(corpus_revision="rev-1"):
    return RetrievalContext(
        retrieval_id="11111111-2222-3333-4444-555555555555",
        captured_at="2026-01-01T00:00:00+00:00",
        corpus_revision=corpus_revision,
        trace_ref={"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True},
        integrity={
            "original_bytes": 0,
            "truncated": False,
            "incomplete": False,
            "observed_bytes": 0,
            "redaction_version": "v0",
        },
    )


def make_result(index=1, **overrides):
    defaults = dict(
        document_id=f"doc-{index}",
        chunk_id=f"chunk-{index}",
        score=0.5 + index / 100,
        snippet=f"snippet {index}",
        source={"kind": "upload"},
        tags={"topic": "rag"},
        metadata={"ordinal": index},
        document_revision=f"drev-{index}",
        chunk_content_hash=f"chash-{index}",
    )
    defaults.update(overrides)
    return RetrievalResult(**defaults)


def make_capture(store, enabled=True):
    return EvaluationCapture(store, enabled=enabled)


def assert_counter_invariant(counters):
    assert counters["captured_count"] <= counters["selected_count"] <= counters["eligible_count"]
    assert (
        counters["selected_count"]
        == counters["captured_count"]
        + counters["capture_error_count"]
        + counters["queue_full_count"]
    )


# ---------------------------------------------------------------------------
# 成功样本投影
# ---------------------------------------------------------------------------


def test_success_sample_projection_keeps_first_ten_with_original_rank():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    results = [make_result(index=i) for i in range(1, 13)]

    capture.capture_success(make_context(), "q", None, 5, results)

    assert len(store.register_calls) == 1
    call = store.register_calls[0]
    payload = call["payload"]
    assert set(payload.keys()) == SAMPLE_ENVELOPE_KEYS
    assert payload["service"] == "n-kb"
    assert payload["type"] == payload["schema_version"] == "evaluation.sample.v1"
    assert payload["query"] == "q"
    assert payload["top_k"] == 5
    assert payload["corpus_revision"] == "rev-1"
    assert payload["retrieval_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["captured_at"] == "2026-01-01T00:00:00+00:00"
    assert len(payload["results"]) == SAMPLE_MAX_RESULTS
    assert [entry["rank"] for entry in payload["results"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert payload["results"][0]["document_id"] == "doc-1"
    assert payload["results"][-1]["document_id"] == "doc-10"
    for entry in payload["results"]:
        assert set(entry.keys()) == RESULT_ENTRY_KEYS
        assert entry["similarity_score"] == pytest.approx(entry["rank"] / 100 + 0.5)
    assert call["max_rows"] == OUTBOX_MAX_ROWS == 10000
    assert call["max_bytes"] == OUTBOX_MAX_BYTES == 256 * 1024 * 1024
    assert call["delta"] == CAPTURED_DELTA
    assert store.counters["captured_count"] == 1
    assert_counter_invariant(store.counters)


def test_producer_event_id_is_uuid4_and_unique_per_attempt():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result()])
    capture.capture_failure(make_context(), "q", None, 5, RuntimeError("boom"))

    first_id = store.register_calls[0]["payload"]["producer_event_id"]
    second_id = store.register_calls[1]["payload"]["producer_event_id"]
    assert first_id != second_id
    for event_id in (first_id, second_id):
        assert str(uuid.UUID(event_id)) == event_id


def test_context_identity_is_never_regenerated_in_capture():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    context = make_context()

    capture.capture_success(context, "q", None, 5, [make_result()])

    payload = store.register_calls[0]["payload"]
    assert payload["retrieval_id"] == context.retrieval_id
    assert payload["captured_at"] == context.captured_at
    assert payload["corpus_revision"] == context.corpus_revision
    assert payload["trace_ref"] == context.trace_ref


def test_filters_projection_includes_min_score():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    filters = RetrievalFilter(
        tags={"topic": "rag"}, source_kind="upload", document_status="indexed", min_score=0.5
    )

    capture.capture_success(make_context(), "q", filters, 5, [make_result()])

    payload = store.register_calls[0]["payload"]
    assert payload["filters"] == {
        "tags": {"topic": "rag"},
        "source_kind": "upload",
        "document_status": "indexed",
        "min_score": 0.5,
    }


def test_none_filters_project_to_empty_object():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result()])

    assert store.register_calls[0]["payload"]["filters"] == {}


# ---------------------------------------------------------------------------
# 投影裁剪与样本 integrity
# ---------------------------------------------------------------------------


def test_no_clipping_integrity_has_equal_bytes_and_false_flags():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result()])

    integrity = store.register_calls[0]["payload"]["integrity"]
    assert integrity["truncated"] is False
    assert integrity["incomplete"] is False
    assert integrity["original_bytes"] == integrity["observed_bytes"] > 0
    assert integrity["redaction_version"] == "v0"


def test_snippet_truncated_to_4096_utf8_bytes_without_splitting_multibyte_char():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    raw = "好" * 2000  # 6000 UTF-8 字节，4096 落在多字节字符中间
    results = [make_result(snippet=raw)]

    capture.capture_success(make_context(), "q", None, 5, results)

    payload = store.register_calls[0]["payload"]
    entry = payload["results"][0]
    encoded = entry["snippet"].encode("utf-8")
    assert len(encoded) <= SNIPPET_MAX_BYTES == 4096
    # 安全截断：是原始字节的前缀，且不浪费超过一个完整字符的字节空间
    assert encoded == raw.encode("utf-8")[: len(encoded)]
    assert SNIPPET_MAX_BYTES - len(encoded) < 3
    assert "�" not in entry["snippet"]
    integrity = payload["integrity"]
    assert integrity["truncated"] is True
    assert integrity["incomplete"] is True
    assert integrity["original_bytes"] > integrity["observed_bytes"]


def test_results_over_1mib_are_tail_clipped(monkeypatch):
    # snippet 截断（4096B）先于 1MiB 结果裁剪，10 条截断后不可能超 1MiB；
    # 放宽 SNIPPET_MAX_BYTES 以单独验证 1MiB 从尾部减条路径（防御纵深）。
    monkeypatch.setattr(
        "app.application.evaluation_capture.SNIPPET_MAX_BYTES", SNIPPET_MAX_BYTES * 100
    )
    store = FakeEvaluationStore()
    capture = make_capture(store)
    results = [make_result(index=i, snippet="x" * 200_000) for i in range(1, 11)]

    capture.capture_success(make_context(), "q", None, 5, results)

    payload = store.register_calls[0]["payload"]
    assert len(payload["results"]) < 10
    observed = len(canonical_json_bytes({"results": payload["results"]}))
    assert observed <= RESULTS_MAX_BYTES == 1024 * 1024
    integrity = payload["integrity"]
    assert integrity["truncated"] is True
    assert integrity["incomplete"] is True
    assert integrity["observed_bytes"] == observed
    # 保留条目的 rank 保留原始位次（从尾部减条，rank 前缀连续）
    kept = len(payload["results"])
    assert [entry["rank"] for entry in payload["results"]] == list(range(1, kept + 1))


def test_truncation_does_not_mutate_caller_results():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    results = [make_result(snippet="长" * 3000)]

    capture.capture_success(make_context(), "q", None, 5, results)

    assert len(results[0].snippet.encode("utf-8")) == 9000


# ---------------------------------------------------------------------------
# 2MiB 信封预算
# ---------------------------------------------------------------------------


def test_untrimmable_envelope_overflow_skips_sample_with_capture_error():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    query = "q" * (ENVELOPE_MAX_BYTES + 1)

    capture.capture_success(make_context(), query, None, 5, [make_result()])

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]
    assert store.counters["capture_error_count"] == 1
    assert_counter_invariant(store.counters)


def test_envelope_tail_clipping_reduces_results_and_recomputes_integrity():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    # 空 results 信封（含全部不可裁剪字段）不超限；带 10 条结果则超限 → 从尾部减条
    query = "q" * (ENVELOPE_MAX_BYTES - 3000)
    results = [make_result(index=i, snippet="y" * 200) for i in range(1, 11)]

    capture.capture_success(make_context(), query, None, 5, results)

    payload = store.register_calls[0]["payload"]
    assert len(payload["results"]) < 10
    envelope = len(canonical_json_bytes(payload))
    assert envelope <= ENVELOPE_MAX_BYTES == 2 * 1024 * 1024
    observed = len(canonical_json_bytes({"results": payload["results"]}))
    integrity = payload["integrity"]
    assert integrity["truncated"] is True
    assert integrity["incomplete"] is True
    assert integrity["observed_bytes"] == observed
    assert [entry["rank"] for entry in payload["results"]] == list(
        range(1, len(payload["results"]) + 1)
    )


# ---------------------------------------------------------------------------
# 身份完整性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_id": None},
        {"document_id": ""},
        {"chunk_id": None},
        {"document_revision": None},
        {"document_revision": ""},
        {"chunk_content_hash": None},
        {"chunk_content_hash": ""},
    ],
)
def test_missing_identity_on_kept_result_skips_whole_sample_with_capture_error(overrides):
    store = FakeEvaluationStore()
    capture = make_capture(store)
    results = [make_result(index=1), make_result(index=2, **overrides)]

    capture.capture_success(make_context(), "q", None, 5, results)

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]
    assert_counter_invariant(store.counters)


def test_identity_beyond_keep_window_does_not_skip_sample():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    # 第 11 条（超出前 10 保留窗口）缺身份不影响样本
    results = [make_result(index=i) for i in range(1, 11)]
    results.append(make_result(index=11, document_revision=None))

    capture.capture_success(make_context(), "q", None, 5, results)

    assert len(store.register_calls) == 1
    assert store.increment_calls == []
    assert len(store.register_calls[0]["payload"]["results"]) == 10


# ---------------------------------------------------------------------------
# corpus_revision 缺失与非有限 score
# ---------------------------------------------------------------------------


def test_missing_corpus_revision_skips_capture_with_capture_error():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_success(make_context(corpus_revision=None), "q", None, 5, [make_result()])

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]


def test_non_finite_score_goes_to_capture_error():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result(score=float("nan"))])

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]


def test_unserializable_filter_value_goes_to_capture_error():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    filters = RetrievalFilter(tags={"topic": object()})

    capture.capture_success(make_context(), "q", filters, 5, [make_result()])

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]


def test_unexpected_projection_fault_never_propagates_and_counts_capture_error():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    class ExplodingFilter:
        @property
        def tags(self):
            raise RuntimeError("sdk exploded")

    # 意外故障不向业务抛出（不依赖 service 侧 _safe_call 兜底）
    capture.capture_success(make_context(), "q", ExplodingFilter(), 5, [make_result()])
    capture.capture_failure(make_context(), "q", ExplodingFilter(), 5, RuntimeError("x"))

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA, CAPTURE_ERROR_DELTA]
    assert_counter_invariant(store.counters)


# ---------------------------------------------------------------------------
# 失败检索捕获
# ---------------------------------------------------------------------------


def test_failure_capture_registers_empty_results_with_same_context_identity():
    store = FakeEvaluationStore()
    capture = make_capture(store)
    context = make_context(corpus_revision="rev-at-start")
    exc = RuntimeError("vector index down")

    capture.capture_failure(context, "q", None, 5, exc)

    assert len(store.register_calls) == 1
    payload = store.register_calls[0]["payload"]
    assert payload["results"] == []
    assert payload["corpus_revision"] == "rev-at-start"
    assert payload["retrieval_id"] == context.retrieval_id
    assert payload["captured_at"] == context.captured_at
    integrity = payload["integrity"]
    assert integrity["truncated"] is False
    assert integrity["incomplete"] is False
    assert integrity["original_bytes"] == integrity["observed_bytes"]
    assert store.register_calls[0]["delta"] == CAPTURED_DELTA
    assert_counter_invariant(store.counters)


def test_failure_capture_with_missing_corpus_revision_skips():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.capture_failure(make_context(corpus_revision=None), "q", None, 5, RuntimeError("x"))

    assert store.register_calls == []
    assert store.increment_calls == [CAPTURE_ERROR_DELTA]


# ---------------------------------------------------------------------------
# 容量拒绝与计数事务不变量
# ---------------------------------------------------------------------------


def test_capacity_rejection_counts_queue_full_exactly_once_via_store():
    store = FakeEvaluationStore()
    store.reject_with_capacity = True
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result()])

    # capture 层不再补记：计数仅来自 store 拒绝事务内部
    assert store.increment_calls == []
    assert len(store.register_calls) == 1
    assert store.register_calls[0]["delta"] == CAPTURED_DELTA
    assert store.counters["eligible_count"] == 1
    assert store.counters["selected_count"] == 1
    assert store.counters["queue_full_count"] == 1
    assert store.counters["captured_count"] == 0
    assert store.counters["capture_error_count"] == 0
    assert_counter_invariant(store.counters)


def test_register_store_fault_falls_back_to_capture_error_increment():
    store = FakeEvaluationStore()
    store.fail_register = True
    capture = make_capture(store)

    capture.capture_success(make_context(), "q", None, 5, [make_result()])

    assert store.increment_calls == [CAPTURE_ERROR_DELTA]
    assert store.counters["capture_error_count"] == 1
    assert_counter_invariant(store.counters)


def test_increment_fallback_fault_is_swallowed_without_payload_leak():
    store = FakeEvaluationStore()
    store.fail_register = True
    store.fail_increment = True
    capture = make_capture(store)

    # 补记也失败：只记健康日志，不向业务抛出
    capture.capture_success(make_context(), "q", None, 5, [make_result()])

    assert len(store.increment_calls) == 1
    assert_counter_invariant(store.counters)


# ---------------------------------------------------------------------------
# 开关关闭与 hook 协议
# ---------------------------------------------------------------------------


def test_disabled_capture_never_touches_store():
    store = FakeEvaluationStore()
    capture = make_capture(store, enabled=False)

    capture.begin_attempt()
    capture.capture_success(make_context(), "q", None, 5, [make_result()])
    capture.capture_failure(make_context(), "q", None, 5, RuntimeError("x"))

    assert store.register_calls == []
    assert store.increment_calls == []


def test_begin_attempt_does_not_write_store():
    store = FakeEvaluationStore()
    capture = make_capture(store)

    capture.begin_attempt()

    assert store.register_calls == []
    assert store.increment_calls == []


def test_wiring_with_retrieval_service_registers_one_sample_per_search():
    from tests.application.test_retrieval_service import (
        FakeEmbeddingProvider,
        FakeVectorIndex,
    )

    class IdentityVectorIndex(FakeVectorIndex):
        def __init__(self):
            super().__init__()
            self.results = [
                make_result(index=1),
                make_result(index=2),
            ]

    store = FakeEvaluationStore()
    capture = make_capture(store)
    service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=IdentityVectorIndex(),
        corpus_revision=_FixedRevision("rev-1"),
        capture_hook=capture,
    )

    outcome = service.search_with_context("rag service", RetrievalFilter(), top_k=3)

    assert len(store.register_calls) == 1
    payload = store.register_calls[0]["payload"]
    assert payload["retrieval_id"] == outcome.context.retrieval_id
    assert payload["corpus_revision"] == "rev-1"
    assert payload["results"][0]["document_id"] == "doc-1"
    assert payload["results"][0]["rank"] == 1
    assert payload["results"][1]["document_id"] == "doc-2"
    assert store.register_calls[0]["delta"] == CAPTURED_DELTA
    assert_counter_invariant(store.counters)


def test_wiring_with_retrieval_service_failure_registers_empty_sample():
    from tests.application.test_retrieval_service import (
        FakeEmbeddingProvider,
        FailingVectorIndex,
    )

    store = FakeEvaluationStore()
    capture = make_capture(store)
    service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=FailingVectorIndex(),
        corpus_revision=_FixedRevision("rev-1"),
        capture_hook=capture,
    )

    with pytest.raises(RuntimeError):
        service.search_with_context("rag service", RetrievalFilter(), top_k=3)

    assert len(store.register_calls) == 1
    payload = store.register_calls[0]["payload"]
    assert payload["results"] == []
    assert payload["corpus_revision"] == "rev-1"
    assert_counter_invariant(store.counters)


class _FixedRevision:
    def __init__(self, revision):
        self._revision = revision

    def current(self):
        return self._revision
