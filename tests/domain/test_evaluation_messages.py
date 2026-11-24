# tests/domain/test_evaluation_messages.py
"""评估消息构造 domain 测试：信封白名单、canonical JSON、完整性、校验规则。"""
import json

import pytest

from app.domain.evaluation import (
    SAMPLE_MESSAGE_TYPE,
    STATE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    EvaluationMessageError,
    build_sample_message,
    build_state_message,
    build_target_deleted_message,
    canonical_json_bytes,
    compute_integrity,
)

VALID_SAMPLE_INTEGRITY = {
    "original_bytes": 2,
    "truncated": False,
    "incomplete": False,
    "observed_bytes": 2,
    "redaction_version": "v0",
}

VALID_STATE_COUNTS = {
    "eligible_count": 1,
    "selected_count": 1,
    "captured_count": 1,
    "capture_error_count": 0,
    "queue_full_count": 0,
}


def make_valid_sample_kwargs(**overrides):
    kwargs = {
        "producer_event_id": "evt-1",
        "retrieval_id": "r-1",
        "captured_at": "2026-11-24T00:00:00+00:00",
        "query": "q",
        "top_k": 5,
        "filters": {"tags": {}, "min_score": 0.5},
        "results": [
            {
                "document_id": "d-1",
                "document_revision": "rev-1",
                "chunk_id": "c-1",
                "chunk_content_hash": "hash-1",
                "rank": 1,
            }
        ],
        "corpus_revision": "rev",
        "trace_ref": None,
        "integrity": dict(VALID_SAMPLE_INTEGRITY),
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def valid_sample_kwargs():
    return make_valid_sample_kwargs()


class TestSampleMessage:
    def test_sample_message_envelope(self, valid_sample_kwargs):
        msg = build_sample_message(**valid_sample_kwargs)
        assert msg["type"] == msg["schema_version"] == SAMPLE_MESSAGE_TYPE
        assert msg["service"] == "n-kb"
        required = {
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
        assert set(msg) == required

    def test_sample_result_requires_identity_keys(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(
                **{**valid_sample_kwargs, "results": [{"document_id": "d", "chunk_id": "c"}]}
            )

    def test_sample_result_keys_pass_when_complete(self, valid_sample_kwargs):
        msg = build_sample_message(**valid_sample_kwargs)
        assert msg["results"] == valid_sample_kwargs["results"]

    def test_naive_timestamp_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "captured_at": "2026-11-24T00:00:00"})

    def test_aware_timestamp_with_offset_accepted(self, valid_sample_kwargs):
        msg = build_sample_message(
            **{**valid_sample_kwargs, "captured_at": "2026-11-24T08:00:00+08:00"}
        )
        assert msg["captured_at"] == "2026-11-24T08:00:00+08:00"

    def test_invalid_timestamp_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "captured_at": "not-a-time"})

    def test_non_mapping_filters_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "filters": ["x"]})

    def test_bool_top_k_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "top_k": True})

    def test_results_non_list_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "results": {"d": "x"}})

    def test_integrity_non_mapping_rejected(self, valid_sample_kwargs):
        with pytest.raises(EvaluationMessageError):
            build_sample_message(**{**valid_sample_kwargs, "integrity": ["i"]})

    def test_serializes_with_canonical_json(self, valid_sample_kwargs):
        msg = build_sample_message(**valid_sample_kwargs)
        encoded = canonical_json_bytes(msg)
        assert json.loads(encoded.decode("utf-8")) == msg


class TestStateMessage:
    def test_state_message_full_whitelist(self):
        msg = build_state_message(
            counter_epoch="e",
            sequence=1,
            counts=dict(VALID_STATE_COUNTS),
            observed_at="2026-11-24T00:00:00+00:00",
        )
        assert msg["type"] == msg["schema_version"] == STATE_MESSAGE_TYPE
        assert msg["producer_id"] == "n-kb"
        assert msg["service"] == "n-kb"
        required = {
            "type",
            "schema_version",
            "service",
            "producer_id",
            "counter_epoch",
            "sequence",
            "eligible_count",
            "selected_count",
            "captured_count",
            "capture_error_count",
            "queue_full_count",
            "observed_at",
        }
        assert set(msg) == required

    def test_state_rejects_missing_count_key(self):
        counts = dict(VALID_STATE_COUNTS)
        counts.pop("queue_full_count")
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence=1,
                counts=counts,
                observed_at="2026-11-24T00:00:00+00:00",
            )

    def test_state_rejects_negative_count(self):
        counts = dict(VALID_STATE_COUNTS)
        counts["captured_count"] = -1
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence=1,
                counts=counts,
                observed_at="2026-11-24T00:00:00+00:00",
            )

    def test_state_rejects_bool_count(self):
        counts = dict(VALID_STATE_COUNTS)
        counts["captured_count"] = True
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence=1,
                counts=counts,
                observed_at="2026-11-24T00:00:00+00:00",
            )

    def test_state_rejects_naive_observed_at(self):
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence=1,
                counts=dict(VALID_STATE_COUNTS),
                observed_at="2026-11-24T00:00:00",
            )

    def test_state_sequence_must_be_int(self):
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence="1",
                counts=dict(VALID_STATE_COUNTS),
                observed_at="2026-11-24T00:00:00+00:00",
            )

    def test_state_rejects_bool_sequence(self):
        with pytest.raises(EvaluationMessageError):
            build_state_message(
                counter_epoch="e",
                sequence=True,
                counts=dict(VALID_STATE_COUNTS),
                observed_at="2026-11-24T00:00:00+00:00",
            )

    def test_state_missing_sequence_rejected(self):
        with pytest.raises(TypeError):
            build_state_message(
                counter_epoch="e",
                counts=dict(VALID_STATE_COUNTS),
                observed_at="2026-11-24T00:00:00+00:00",
            )


class TestTargetDeletedMessage:
    def test_deleted_message_whitelist(self):
        msg = build_target_deleted_message(
            document_id="d-1", document_revision="rev-1", reason="document_deleted"
        )
        assert msg["type"] == msg["schema_version"] == TARGET_DELETED_MESSAGE_TYPE
        assert msg["service"] == "n-kb"
        assert set(msg) == {
            "type",
            "schema_version",
            "service",
            "document_id",
            "document_revision",
            "reason",
        }

    def test_deleted_message_carries_fields(self):
        msg = build_target_deleted_message(
            document_id="d-1", document_revision="rev-1", reason="document_deleted"
        )
        assert msg["document_id"] == "d-1"
        assert msg["document_revision"] == "rev-1"
        assert msg["reason"] == "document_deleted"


class TestCanonicalJson:
    def test_keys_sorted_and_compact(self):
        encoded = canonical_json_bytes({"b": 1, "a": 2})
        assert encoded == b'{"a":2,"b":1}'

    def test_utf8_non_ascii(self):
        encoded = canonical_json_bytes({"k": "中文"})
        assert encoded == '{"k":"中文"}'.encode("utf-8")

    def test_rejects_nan(self):
        with pytest.raises(EvaluationMessageError):
            canonical_json_bytes({"x": float("nan")})

    def test_rejects_infinity(self):
        with pytest.raises(EvaluationMessageError):
            canonical_json_bytes({"x": float("inf")})

    def test_rejects_non_json_native_types(self):
        with pytest.raises(EvaluationMessageError):
            canonical_json_bytes({"x": object()})

    def test_rejects_non_mapping_input(self):
        with pytest.raises(EvaluationMessageError):
            canonical_json_bytes(["not", "a", "mapping"])


class TestComputeIntegrity:
    def test_returns_full_dict(self):
        integrity = compute_integrity(
            b"original", b"observed", truncated=True, incomplete=False
        )
        assert integrity == {
            "original_bytes": 8,
            "truncated": True,
            "incomplete": False,
            "observed_bytes": 8,
            "redaction_version": "v0",
        }

    def test_empty_payloads(self):
        integrity = compute_integrity(b"", b"", truncated=False, incomplete=False)
        assert integrity["original_bytes"] == 0
        assert integrity["observed_bytes"] == 0
