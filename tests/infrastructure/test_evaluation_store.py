"""评估存储 infrastructure 测试：outbox 容量与租约、计数器心跳、语料状态单行 KV、写路径置脏接线。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.evaluation import (
    SAMPLE_MESSAGE_TYPE,
    STATE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    OutboxCapacityError,
    build_sample_message,
    build_target_deleted_message,
    canonical_json_bytes,
)
from app.domain.tag import Tag
from app.infrastructure.persistence.sqlite_store import SQLiteStore

CAPACITY_ROW_LIMIT = 100
CAPACITY_BYTE_LIMIT = 10**9
DEFAULT_MAX_ROWS = 10
DEFAULT_MAX_BYTES = 10**9


# ---------------------------------------------------------------------------
# fixtures 与构造辅助
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "n-kb.db")


def sample_payload(event_id="evt-1"):
    return build_sample_message(
        producer_event_id=event_id,
        retrieval_id=f"r-{event_id}",
        captured_at="2026-01-01T00:00:00+00:00",
        query="q",
        top_k=3,
        filters={},
        results=[],
        corpus_revision="rev-1",
        trace_ref={},
        integrity={
            "original_bytes": 1,
            "truncated": False,
            "incomplete": False,
            "observed_bytes": 1,
            "redaction_version": "v0",
        },
    )


def tombstone_payload(document_id="doc-1"):
    return build_target_deleted_message(
        document_id=document_id, document_revision="rev-1", reason="deleted"
    )


def make_document(document_id="doc-1", status=DocumentStatus.INDEXED, content_hash="hash-1"):
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title="notes.md",
        source=DocumentSource(kind="upload", uri="notes.md", display_name="Notes", metadata={}),
        content_hash=content_hash,
        content_type="text/markdown",
        size_bytes=12,
        status=status,
        created_at=now,
        updated_at=now,
    )


def make_content(document_id="doc-1", content_hash="hash-1"):
    return DocumentContent(
        document_id=document_id,
        text="hello sqlite",
        content_hash=content_hash,
        encoding="utf-8",
        created_at=datetime.now(timezone.utc),
    )


def outbox_row(store, producer_event_id):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT * FROM evaluation_outbox WHERE producer_event_id = ?", (producer_event_id,)
        ).fetchone()
    return row


def claim_now():
    return datetime.now(timezone.utc)


def make_dead_letter_sample(store, event_id="evt-1"):
    """登记一个 sample 并通过投递失败转为 dead_letter（30s 租约便于过期测试）。"""
    store.register_outbox_message(sample_payload(event_id), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    store.claim_due_outbox(owner="w1", lease_seconds=30, now=claim_now())
    store.record_delivery_attempt(
        event_id,
        delete=False,
        status="dead_letter",
        attempts=1,
        next_retry_at=None,
        last_error="boom",
        owner="w1",
    )


# ---------------------------------------------------------------------------
# schema：幂等建表与旧库无损迁移
# ---------------------------------------------------------------------------


def _table_names(store):
    with store._connect() as connection:
        return {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_schema_creates_three_evaluation_tables(tmp_path):
    store = SQLiteStore(tmp_path / "n-kb.db")

    tables = _table_names(store)

    assert {"evaluation_outbox", "evaluation_counters", "evaluation_corpus_state"} <= tables


def test_schema_init_is_idempotent_and_old_database_migrates_without_loss(tmp_path):
    path = tmp_path / "n-kb.db"
    store = SQLiteStore(path)
    document = make_document()
    store.save_document(document, make_content(document.id), [Tag("topic", "rag")])

    # 模拟旧库：删除新表与新索引后重新初始化
    with store._connect() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_eval_outbox_status_retry")
        connection.execute("DROP INDEX IF EXISTS idx_eval_outbox_control_key")
        connection.execute("DROP TABLE IF EXISTS evaluation_outbox")
        connection.execute("DROP TABLE IF EXISTS evaluation_counters")
        connection.execute("DROP TABLE IF EXISTS evaluation_corpus_state")

    rebuilt = SQLiteStore(path)

    assert {"evaluation_outbox", "evaluation_counters", "evaluation_corpus_state"} <= _table_names(rebuilt)
    assert rebuilt.get_document("doc-1") == document
    assert rebuilt.get_tags("doc-1") == [Tag("topic", "rag")]
    rebuilt.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    assert outbox_row(rebuilt, "evt-1") is not None


# ---------------------------------------------------------------------------
# outbox 登记：canonical 存储、容量上限
# ---------------------------------------------------------------------------


def test_register_stores_canonical_payload_and_byte_size(store):
    payload = sample_payload("evt-1")
    canonical = canonical_json_bytes(payload)

    store.register_outbox_message(payload, max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)

    row = outbox_row(store, "evt-1")
    assert row["payload_json"] == canonical.decode("utf-8")
    assert row["payload_bytes"] == len(canonical)
    assert row["message_type"] == SAMPLE_MESSAGE_TYPE
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_retry_at"] is None
    assert row["owner"] is None


def test_register_enforces_row_cap(store):
    for i in range(CAPACITY_ROW_LIMIT):
        store.register_outbox_message(
            sample_payload(f"evt-{i}"), max_rows=CAPACITY_ROW_LIMIT, max_bytes=CAPACITY_BYTE_LIMIT
        )

    with pytest.raises(OutboxCapacityError):
        store.register_outbox_message(
            sample_payload("evt-100"), max_rows=CAPACITY_ROW_LIMIT, max_bytes=CAPACITY_BYTE_LIMIT
        )


def test_register_enforces_byte_cap(store):
    payload_size = len(canonical_json_bytes(sample_payload("evt-0")))
    max_bytes = payload_size * 3

    store.register_outbox_message(sample_payload("evt-0"), max_rows=DEFAULT_MAX_ROWS, max_bytes=max_bytes)
    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=max_bytes)
    # 恰好等于上限允许
    store.register_outbox_message(sample_payload("evt-2"), max_rows=DEFAULT_MAX_ROWS, max_bytes=max_bytes)

    with pytest.raises(OutboxCapacityError):
        store.register_outbox_message(sample_payload("evt-3"), max_rows=DEFAULT_MAX_ROWS, max_bytes=max_bytes)


def test_dead_letter_rows_still_count_toward_capacity(store):
    make_dead_letter_sample(store, "evt-1")

    with pytest.raises(OutboxCapacityError):
        store.register_outbox_message(sample_payload("evt-2"), max_rows=1, max_bytes=DEFAULT_MAX_BYTES)


# ---------------------------------------------------------------------------
# outbox 领取：租约 CAS、dead_letter 隔离
# ---------------------------------------------------------------------------


def test_claim_due_lease_atomicity(store):
    store.register_outbox_message(sample_payload("evt-1"), max_rows=CAPACITY_ROW_LIMIT, max_bytes=CAPACITY_BYTE_LIMIT)

    first = store.claim_due_outbox(owner="w1", lease_seconds=30, now=claim_now())
    assert first[0]["producer_event_id"] == "evt-1"
    assert first[0]["owner"] == "w1"
    assert first[0]["payload"]["producer_event_id"] == "evt-1"

    # 租约未过期：其他 owner 领取不到
    assert store.claim_due_outbox(owner="w2", lease_seconds=30, now=claim_now()) == []

    # 租约过期后可被重新领取
    later = store.claim_due_outbox(owner="w2", lease_seconds=30, now=claim_now() + timedelta(seconds=31))
    assert later[0]["producer_event_id"] == "evt-1"
    assert later[0]["owner"] == "w2"


def test_claim_due_skips_rows_with_future_next_retry_at(store):
    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    store.claim_due_outbox(owner="w1", lease_seconds=30, now=claim_now())
    retry_at = (claim_now() + timedelta(seconds=60)).isoformat()
    store.record_delivery_attempt(
        "evt-1",
        delete=False,
        status="pending",
        attempts=1,
        next_retry_at=retry_at,
        last_error="boom",
        owner="w1",
    )

    row = outbox_row(store, "evt-1")
    assert row["attempts"] == 1
    assert row["next_retry_at"] == retry_at
    # 租约未过期且重试未到期：不可领取
    assert store.claim_due_outbox(owner="w2", lease_seconds=30, now=claim_now()) == []

    later = store.claim_due_outbox(owner="w2", lease_seconds=30, now=claim_now() + timedelta(seconds=61))
    assert [message["producer_event_id"] for message in later] == ["evt-1"]
    assert later[0]["attempts"] == 1


def test_dead_letter_not_claimed_until_replayed(store):
    make_dead_letter_sample(store, "evt-1")

    assert store.claim_due_outbox(owner="w2", lease_seconds=30, now=claim_now()) == []

    assert store.replay_dead_letter(None) == 1
    replayed = store.claim_due_outbox(owner="w3", lease_seconds=30, now=claim_now() + timedelta(seconds=31))
    assert [message["producer_event_id"] for message in replayed] == ["evt-1"]

    row = outbox_row(store, "evt-1")
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] is None


def test_replay_dead_letter_targets_specific_ids(store):
    make_dead_letter_sample(store, "evt-1")
    make_dead_letter_sample(store, "evt-2")

    assert store.replay_dead_letter(["evt-1"]) == 1
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT producer_event_id, status FROM evaluation_outbox ORDER BY producer_event_id"
        ).fetchall()
    assert {row["producer_event_id"]: row["status"] for row in rows} == {
        "evt-1": "pending",
        "evt-2": "dead_letter",
    }
    assert store.replay_dead_letter(["missing"]) == 0


def test_purge_dead_letter_deletes_rows(store):
    make_dead_letter_sample(store, "evt-1")
    store.register_outbox_message(sample_payload("evt-2"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)

    assert store.purge_dead_letter(["evt-1"]) == 1
    assert outbox_row(store, "evt-1") is None
    assert outbox_row(store, "evt-2") is not None
    assert store.purge_dead_letter(None) == 0


# ---------------------------------------------------------------------------
# 投递结果回写：owner + 租约 CAS
# ---------------------------------------------------------------------------


def test_record_delivery_attempt_rejected_when_lease_expired(store):
    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())
    with store._connect() as connection:
        connection.execute(
            "UPDATE evaluation_outbox SET lease_until = ?",
            ((claim_now() - timedelta(seconds=1)).isoformat(),),
        )

    store.record_delivery_attempt(
        "evt-1",
        delete=False,
        status="pending",
        attempts=1,
        next_retry_at=(claim_now() + timedelta(seconds=5)).isoformat(),
        last_error="boom",
        owner="w1",
    )

    row = outbox_row(store, "evt-1")
    assert row["attempts"] == 0
    assert row["next_retry_at"] is None
    assert row["last_error"] is None
    assert row["status"] == "pending"


def test_record_delivery_attempt_rejected_for_non_holder(store):
    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())

    store.record_delivery_attempt(
        "evt-1",
        delete=False,
        status="pending",
        attempts=1,
        next_retry_at=None,
        last_error="boom",
        owner="w2",
    )

    row = outbox_row(store, "evt-1")
    assert row["attempts"] == 0
    assert row["last_error"] is None


def test_record_delivery_attempt_with_delete_removes_delivered_row(store):
    store.register_control_message(tombstone_payload("doc-1"), control_key="deleted:doc-1")
    store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())

    store.record_delivery_attempt(
        "deleted:doc-1",
        delete=True,
        status=None,
        attempts=1,
        next_retry_at=None,
        last_error=None,
        owner="w1",
    )

    with store._connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM evaluation_outbox WHERE control_key = 'deleted:doc-1'"
        ).fetchone()[0]
    assert remaining == 0


# ---------------------------------------------------------------------------
# 控制消息：确定性 PK、幂等、不受 sample 配额约束、ack/清理
# ---------------------------------------------------------------------------


def test_register_control_message_is_idempotent_and_bypasses_sample_quota(store):
    store.register_outbox_message(sample_payload("evt-1"), max_rows=1, max_bytes=DEFAULT_MAX_BYTES)
    payload = tombstone_payload("doc-1")

    store.register_control_message(payload, control_key="deleted:doc-1")
    store.register_control_message(payload, control_key="deleted:doc-1")

    with store._connect() as connection:
        rows = connection.execute(
            "SELECT producer_event_id, control_key, message_type, payload_json FROM evaluation_outbox "
            "WHERE control_key = 'deleted:doc-1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["producer_event_id"] == "deleted:doc-1"
    assert rows[0]["message_type"] == TARGET_DELETED_MESSAGE_TYPE
    assert "producer_event_id" not in json.loads(rows[0]["payload_json"])

    claimed = store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())
    assert {message["message_type"] for message in claimed} == {
        SAMPLE_MESSAGE_TYPE,
        TARGET_DELETED_MESSAGE_TYPE,
    }


def test_clear_control_message_requires_remote_ack(store):
    store.register_control_message(tombstone_payload("doc-1"), control_key="deleted:doc-1")

    store.clear_control_message("deleted:doc-1")

    assert outbox_row(store, "deleted:doc-1") is not None


def test_mark_remote_acked_requires_matching_unexpired_lease_then_clear_deletes(store):
    store.register_control_message(tombstone_payload("doc-1"), control_key="deleted:doc-1")
    store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())

    # 非 holder 不能 ack
    store.mark_remote_acked("deleted:doc-1", owner="w2", now=claim_now())
    assert outbox_row(store, "deleted:doc-1")["remote_acked"] == 0
    # 租约过期后不能 ack
    store.mark_remote_acked("deleted:doc-1", owner="w1", now=claim_now() + timedelta(seconds=301))
    assert outbox_row(store, "deleted:doc-1")["remote_acked"] == 0

    store.mark_remote_acked("deleted:doc-1", owner="w1", now=claim_now())
    assert outbox_row(store, "deleted:doc-1")["remote_acked"] == 1

    store.clear_control_message("deleted:doc-1")
    assert outbox_row(store, "deleted:doc-1") is None


# ---------------------------------------------------------------------------
# 计数器与心跳
# ---------------------------------------------------------------------------


def test_counters_snapshot_sequence(store):
    delta = {"eligible_count": 1, "selected_count": 1, "capture_error_count": 1}
    store.increment_counters(delta)
    first = json.loads(store.snapshot_heartbeat(observed_at=claim_now().isoformat()))
    store.increment_counters(delta)
    second = json.loads(store.snapshot_heartbeat(observed_at=claim_now().isoformat()))

    assert second["sequence"] == first["sequence"] + 1
    assert second["capture_error_count"] == 2
    assert second["counter_epoch"] == first["counter_epoch"]


def test_increment_counters_does_not_advance_heartbeat_sequence(store):
    store.increment_counters({"eligible_count": 1})
    first = json.loads(store.snapshot_heartbeat(observed_at=claim_now().isoformat()))

    store.increment_counters({"eligible_count": 1})

    counters = store.read_counters()
    assert counters["eligible_count"] == 2
    second = json.loads(store.snapshot_heartbeat(observed_at=claim_now().isoformat()))
    assert second["sequence"] == first["sequence"] + 1


def test_increment_counters_rejects_negative_delta(store):
    with pytest.raises(ValueError):
        store.increment_counters({"eligible_count": -1})


def test_snapshot_heartbeat_initializes_zero_counters(store):
    payload = json.loads(store.snapshot_heartbeat(observed_at=claim_now().isoformat()))

    assert payload["type"] == STATE_MESSAGE_TYPE
    assert payload["sequence"] == 1
    assert payload["counter_epoch"]
    assert payload["eligible_count"] == 0
    assert payload["selected_count"] == 0
    assert payload["captured_count"] == 0
    assert payload["capture_error_count"] == 0
    assert payload["queue_full_count"] == 0


def test_read_counters_returns_none_before_first_counter_write(store):
    assert store.read_counters() is None


def test_register_sample_with_counter_accumulates_and_inserts(store):
    payload = sample_payload("evt-1")

    counters = store.register_sample_with_counter(
        payload,
        {"eligible_count": 1, "selected_count": 1, "captured_count": 1},
        max_rows=DEFAULT_MAX_ROWS,
        max_bytes=DEFAULT_MAX_BYTES,
    )

    assert counters["eligible_count"] == 1
    assert counters["selected_count"] == 1
    assert counters["captured_count"] == 1
    assert outbox_row(store, "evt-1") is not None
    claimed = store.claim_due_outbox(owner="w1", lease_seconds=300, now=claim_now())
    assert [message["producer_event_id"] for message in claimed] == ["evt-1"]


def test_register_sample_with_counter_commits_queue_full_bookkeeping_on_rejection(store):
    payload = sample_payload("evt")
    payload_size = len(canonical_json_bytes(payload))
    max_bytes = payload_size

    store.register_sample_with_counter(
        payload,
        {"eligible_count": 1, "selected_count": 1, "captured_count": 1},
        max_rows=DEFAULT_MAX_ROWS,
        max_bytes=max_bytes,
    )

    with pytest.raises(OutboxCapacityError):
        store.register_sample_with_counter(
            sample_payload("evt-2"),
            {"eligible_count": 1, "selected_count": 1, "captured_count": 1},
            max_rows=DEFAULT_MAX_ROWS,
            max_bytes=max_bytes,
        )

    counters = store.read_counters()
    assert counters["eligible_count"] == 2
    assert counters["selected_count"] == 2
    assert counters["queue_full_count"] == 1
    assert counters["captured_count"] == 1
    assert counters["capture_error_count"] == 0
    # captured <= selected <= eligible
    assert counters["captured_count"] <= counters["selected_count"] <= counters["eligible_count"]
    # 被拒绝的 sample 不落库
    assert outbox_row(store, "evt-2") is None


# ---------------------------------------------------------------------------
# 语料状态：单行 KV、dirty/generation/config_fingerprint、CAS 写入
# ---------------------------------------------------------------------------


def test_read_corpus_state_returns_none_before_first_dirty(store):
    assert store.read_corpus_state() is None


def test_corpus_state_round_trip_and_cas_write(store):
    assert store.mark_corpus_dirty() == 1

    assert store.write_corpus_state({"docs": ["doc-1"]}, expected_generation=1, config_fingerprint="fp-1") is True

    state = store.read_corpus_state()
    assert state["value"] == {"docs": ["doc-1"]}
    assert state["dirty"] is False
    assert state["generation"] == 1
    assert state["config_fingerprint"] == "fp-1"

    # 过期 generation 的 CAS 写入失败且不改值
    assert store.write_corpus_state({"docs": ["doc-2"]}, expected_generation=0, config_fingerprint="fp-0") is False
    assert store.read_corpus_state()["value"] == {"docs": ["doc-1"]}


def test_write_corpus_state_inserts_initial_generation_zero(store):
    assert store.write_corpus_state({"docs": {}}, expected_generation=0, config_fingerprint="fp-0") is True

    state = store.read_corpus_state()
    assert state["value"] == {"docs": {}}
    assert state["dirty"] is False
    assert state["generation"] == 0


def test_mark_corpus_dirty_increments_generation_and_sets_dirty(store):
    store.mark_corpus_dirty()
    assert store.write_corpus_state({"docs": ["doc-1"]}, expected_generation=1, config_fingerprint="fp-1") is True

    generation = store.mark_corpus_dirty()

    assert generation == 2
    state = store.read_corpus_state()
    assert state["dirty"] is True
    assert state["generation"] == 2
    # dirty 不清除已计算的 value
    assert state["value"] == {"docs": ["doc-1"]}


def test_list_indexed_document_hashes_returns_only_indexed_ordered_by_id(store):
    store.save_document(make_document("doc-2", status=DocumentStatus.UPLOADED, content_hash="hash-2"), make_content("doc-2", "hash-2"), [])
    store.save_document(make_document("doc-1", status=DocumentStatus.INDEXED, content_hash="hash-1"), make_content("doc-1", "hash-1"), [])
    store.save_document(make_document("doc-3", status=DocumentStatus.FAILED, content_hash="hash-3"), make_content("doc-3", "hash-3"), [])

    assert store.list_indexed_document_hashes() == {"doc-1": "hash-1"}


def test_read_corpus_snapshot_returns_indexed_hashes_and_generation(store):
    store.save_document(make_document(), make_content(), [])

    # indexed 保存即置脏 -> generation 1
    assert store.read_corpus_snapshot() == ({"doc-1": "hash-1"}, 1)

    store.update_status("doc-1", DocumentStatus.FAILED)

    assert store.read_corpus_snapshot() == ({}, 2)


class _RecordingConnection:
    """代理连接：记录执行的 SQL 语句顺序，其余行为委托真实连接。"""

    def __init__(self, inner):
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql, *args):
        self.statements.append(" ".join(sql.split()))
        return self._inner.execute(sql, *args)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_read_corpus_snapshot_reads_generation_first_inside_explicit_transaction(store, monkeypatch):
    store.save_document(make_document(), make_content(), [])
    recording = _RecordingConnection(store._connect())
    monkeypatch.setattr(store, "_connect", lambda: recording)

    snapshot = store.read_corpus_snapshot()

    assert snapshot == ({"doc-1": "hash-1"}, 1)
    assert recording.statements[0] == "BEGIN"
    generation_index = next(
        index for index, sql in enumerate(recording.statements) if "evaluation_corpus_state" in sql
    )
    documents_index = next(
        index for index, sql in enumerate(recording.statements) if "FROM documents" in sql
    )
    # generation 先读：任何成员变化都会递增 generation，撕裂读只会配对
    # 新文档 + 旧 generation，T3 CAS 失败重试，不会发布过期修订
    assert generation_index < documents_index


def test_claim_and_ack_normalize_non_utc_now_to_utc(store):
    utc_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cst_now = utc_now.astimezone(timezone(timedelta(hours=8)))  # 同一时刻，+08:00 表示
    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)

    store.claim_due_outbox(owner="w1", lease_seconds=30, now=cst_now)

    # 租约按 UTC 规范化存储（微秒精度）
    row = outbox_row(store, "evt-1")
    expected_lease = (utc_now + timedelta(seconds=30)).isoformat(timespec="microseconds")
    assert row["lease_until"] == expected_lease

    # 真实租约到期于 UTC 12:00:30：UTC 12:00:31 可重领（非 UTC 字符串会让字典序比较失效）
    later = store.claim_due_outbox(owner="w2", lease_seconds=30, now=utc_now + timedelta(seconds=31))
    assert [message["owner"] for message in later] == ["w2"]

    # 未过期（UTC 12:01:00 之前）不可领取
    assert store.claim_due_outbox(owner="w3", lease_seconds=30, now=utc_now + timedelta(seconds=1)) == []


def test_mark_remote_acked_normalizes_non_utc_now(store):
    utc_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cst_now = utc_now.astimezone(timezone(timedelta(hours=8)))
    store.register_control_message(tombstone_payload("doc-1"), control_key="deleted:doc-1")
    store.claim_due_outbox(owner="w1", lease_seconds=30, now=cst_now)

    # UTC 13:00 已过真实租约到期（UTC 12:00:30）：即使原始 +08:00 租约字符串更大也必须拒绝
    store.mark_remote_acked("deleted:doc-1", owner="w1", now=utc_now + timedelta(hours=1))
    assert outbox_row(store, "deleted:doc-1")["remote_acked"] == 0

    store.mark_remote_acked("deleted:doc-1", owner="w1", now=utc_now)
    assert outbox_row(store, "deleted:doc-1")["remote_acked"] == 1


# ---------------------------------------------------------------------------
# 捕获专用连接 50ms busy timeout，业务连接不受影响
# ---------------------------------------------------------------------------


def test_capture_writes_use_short_busy_timeout_and_business_connections_keep_default(tmp_path, monkeypatch):
    recorded_timeouts = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout", 5.0))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    store = SQLiteStore(tmp_path / "n-kb.db")

    store.register_outbox_message(sample_payload("evt-1"), max_rows=DEFAULT_MAX_ROWS, max_bytes=DEFAULT_MAX_BYTES)
    store.save_document(make_document(), make_content(), [])

    assert 0.05 in recorded_timeouts
    assert recorded_timeouts.count(5.0) >= 2


# ---------------------------------------------------------------------------
# 写路径接线：save_document / update_status / delete_document 同事务置脏
# ---------------------------------------------------------------------------


def test_write_paths_mark_corpus_dirty_with_generation_bump(store):
    # 非 indexed 保存不置脏
    store.save_document(make_document(status=DocumentStatus.UPLOADED), make_content(), [])
    assert store.read_corpus_state() is None

    # indexed 保存 -> 置脏 generation 1
    store.save_document(make_document(status=DocumentStatus.INDEXED), make_content(), [])
    state = store.read_corpus_state()
    assert state["generation"] == 1
    assert state["dirty"] is True

    # indexed 且内容不变的重复保存不递增
    store.save_document(make_document(status=DocumentStatus.INDEXED), make_content(), [])
    assert store.read_corpus_state()["generation"] == 1

    # indexed 内容更新 -> generation 2
    store.save_document(
        make_document(status=DocumentStatus.INDEXED, content_hash="hash-2"),
        make_content(content_hash="hash-2"),
        [],
    )
    assert store.read_corpus_state()["generation"] == 2

    # indexed -> failed -> generation 3
    store.update_status("doc-1", DocumentStatus.FAILED)
    assert store.read_corpus_state()["generation"] == 3

    # failed -> indexed（重新入索引）-> generation 4
    store.update_status("doc-1", DocumentStatus.INDEXED)
    assert store.read_corpus_state()["generation"] == 4

    # 非 indexed 成员间的状态流转不置脏
    store.save_document(make_document("doc-2", status=DocumentStatus.UPLOADED), make_content("doc-2"), [])
    store.update_status("doc-2", DocumentStatus.INDEXING)
    assert store.read_corpus_state()["generation"] == 4

    # 删除 indexed 文档 -> generation 5，且快照为空
    store.delete_document("doc-1")
    assert store.read_corpus_state()["generation"] == 5

    # 删除非 indexed 文档不递增
    store.delete_document("doc-2")
    assert store.read_corpus_state()["generation"] == 5

    assert store.read_corpus_snapshot() == ({}, 5)


# ---------------------------------------------------------------------------
# T8: delete_document_with_control 单事务删除与控制行清理元数据
# ---------------------------------------------------------------------------


def test_delete_document_with_control_is_atomic(store):
    document = make_document()
    store.save_document(document, make_content(), [])

    deleted = store.delete_document_with_control("doc-1", "user_request")

    # 返回事务内读到的删除前文档
    assert deleted.content_hash == "hash-1"
    assert deleted.id == "doc-1"
    assert store.get_document("doc-1") is None

    # tombstone 控制行同事务登记：payload 为删除前 revision 构造的墓碑消息
    with store._connect() as connection:
        row = connection.execute(
            "SELECT * FROM evaluation_outbox WHERE control_key IS NOT NULL"
        ).fetchone()
    assert row["message_type"] == TARGET_DELETED_MESSAGE_TYPE
    assert json.loads(row["payload_json"]) == build_target_deleted_message(
        document_id="doc-1", document_revision="hash-1", reason="user_request"
    )
    # 控制行出生即 cleanup_pending=1（外部清理尚未执行），未 ACK
    assert row["cleanup_pending"] == 1
    assert row["remote_acked"] == 0
    # corpus dirty 同事务置位并递增 generation（indexed 保存已置 generation 1）
    state = store.read_corpus_state()
    assert state["dirty"] is True
    assert state["generation"] == 2


def test_delete_document_with_control_missing_document_raises(store):
    with pytest.raises(ValueError, match="document_not_found"):
        store.delete_document_with_control("missing", "user_request")


def test_delete_document_with_control_control_key_is_deterministic(store):
    store.save_document(make_document(), make_content(), [])
    store.delete_document_with_control("doc-1", "user_request")

    # 同 document_id/document_revision 重建后再次删除：确定性 PK 幂等，不产生第二行
    store.save_document(make_document(), make_content(), [])
    store.delete_document_with_control("doc-1", "user_request")

    with store._connect() as connection:
        rows = connection.execute(
            "SELECT control_key, payload_json FROM evaluation_outbox WHERE control_key IS NOT NULL"
        ).fetchall()
    assert len(rows) == 1
    # 幂等登记保留首次 payload（首个 reason）
    assert json.loads(rows[0]["payload_json"])["reason"] == "user_request"


def test_delete_document_with_control_rolls_back_on_outbox_failure(store, monkeypatch):
    store.save_document(make_document(), make_content(), [])

    def broken_register(payload, control_key, connection=None, cleanup_pending=0):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(store, "register_control_message", broken_register)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        store.delete_document_with_control("doc-1", "user_request")

    # 任一步失败整体回滚：文档仍在、outbox 无残留、generation 停留在保存时基线 1
    assert store.get_document("doc-1") is not None
    with store._connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM evaluation_outbox").fetchone()[0]
    assert count == 0
    assert store.read_corpus_state()["generation"] == 1


def test_control_cleanup_metadata_round_trip(store):
    store.save_document(make_document(), make_content(), [])
    store.delete_document_with_control("doc-1", "user_request")

    rows = store.list_control_messages()
    assert len(rows) == 1
    listed = rows[0]
    assert listed["payload"]["document_id"] == "doc-1"
    assert listed["payload"]["document_revision"] == "hash-1"
    assert listed["cleanup_pending"] is True
    assert listed["remote_acked"] is False

    # 目标定位回写：清理完成复位 pending，未 ACK 行保留
    store.mark_control_cleanup_pending("doc-1", "hash-1", pending=False)
    rows = store.list_control_messages()
    assert rows[0]["cleanup_pending"] is False
    assert len(rows) == 1

    # remote_acked + cleanup 完成才删行：先 ACK 再 clear
    now = claim_now()
    claimed = store.claim_due_outbox("w1", 45, now)
    assert claimed, "tombstone 应可被领取"
    store.mark_remote_acked(claimed[0]["producer_event_id"], owner="w1", now=now)
    store.mark_control_cleanup_pending("doc-1", "hash-1", pending=False)
    store.clear_control_message(rows[0]["control_key"])
    assert store.list_control_messages() == []


def test_list_control_messages_reports_remote_acked_state(store):
    store.save_document(make_document(), make_content(), [])
    store.delete_document_with_control("doc-1", "user_request")
    control_key = store.list_control_messages()[0]["control_key"]

    remote_ack_tombstone_store_level(store, control_key)

    rows = store.list_control_messages()
    assert rows[0]["remote_acked"] is True
    assert rows[0]["cleanup_pending"] is True


def remote_ack_tombstone_store_level(store, control_key):
    now = claim_now()
    claimed = store.claim_due_outbox("w1", 45, now)
    assert control_key in [row["producer_event_id"] for row in claimed]
    store.mark_remote_acked(control_key, owner="w1", now=now)
