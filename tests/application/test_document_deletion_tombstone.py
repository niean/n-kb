# T8: 删除 tombstone 同事务与外部清理恢复
#
# 覆盖（plan T8 S1）：
# - 注入 outbox 插入失败 -> SQLite 删除回滚（文档仍在）
# - 成功提交原子性：删除前 document_revision 进 tombstone、dirty 置位、
#   sample 满箱仍登记控制消息（控制消息不受 sample 配额约束）
# - Qdrant 清理失败 -> cleanup_pending=1，经恢复入口（retry_pending_cleanups）
#   重试后解除；remote_acked 与本地清理都完成才删控制行
# - 外部残留命中不作为检索结果返回（T4 过滤仍然成立，不重新实现）
# - 重复删除不存在文档幂等报 document_not_found
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.application.document_service import DocumentService
from app.application.evaluation_capture import EvaluationCapture
from app.application.retrieval_service import RetrievalService
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.evaluation import (
    TARGET_DELETED_MESSAGE_TYPE,
    build_sample_message,
    build_target_deleted_message,
)
from app.domain.retrieval import RetrievalResult
from app.infrastructure.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# 测试桩与工具
# ---------------------------------------------------------------------------


class FakeVectorIndex:
    """向量索引桩：可注入 replace_document 故障模拟 Qdrant 清理失败；
    results 支持注入外部残留命中（SQLite 已删除但向量仍在）。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.replaced = []
        self.results = []

    def replace_document(self, document_id, chunks, vectors, tags, source):
        if self.fail:
            raise RuntimeError("qdrant unavailable")
        self.replaced.append((document_id, chunks, vectors, tags, source))

    def search(self, vector, filters, top_k):
        return self.results

    def health(self):
        return {"status": "ok"}


class FakeObjectStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = []

    def save(self, document_id, filename, content):
        raise NotImplementedError("tombstone 测试不走上传路径")

    def read_text(self, document_id):
        return ""

    def delete(self, document_id):
        if self.fail:
            raise RuntimeError("object store unavailable")
        self.deleted.append(document_id)


class FakeEmbeddingProvider:
    def embed_chunks(self, chunks):
        return []

    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


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
        text="hello tombstone",
        content_hash=content_hash,
        encoding="utf-8",
        created_at=datetime.now(timezone.utc),
    )


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


def make_service(store, vector_index=None, object_store=None):
    """构造注入原子删除端口（evaluation_control_register）的 DocumentService。"""
    return DocumentService(
        document_repository=store,
        object_store=object_store or FakeObjectStore(),
        max_upload_bytes=1024,
        vector_index=vector_index,
        evaluation_control_register=store,
    )


def control_rows(store):
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT * FROM evaluation_outbox WHERE control_key IS NOT NULL ORDER BY created_at"
        ).fetchall()
    return rows


def outbox_count(store):
    with store._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM evaluation_outbox").fetchone()[0]


def remote_ack_tombstone(store, control_key, owner="w1"):
    """按真实投递路径模拟远端严格 ACK：先租约领取再 mark_remote_acked。"""
    now = datetime.now(timezone.utc)
    claimed = store.claim_due_outbox(owner, 45, now)
    event_ids = [row["producer_event_id"] for row in claimed]
    assert control_key in event_ids, f"tombstone 未被领取: {event_ids}"
    store.mark_remote_acked(control_key, owner=owner, now=now)


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "n-kb.db")


# ---------------------------------------------------------------------------
# 同事务原子性
# ---------------------------------------------------------------------------


def test_delete_document_registers_tombstone_with_pre_delete_revision(store):
    store.save_document(make_document(), make_content(), [])
    service = make_service(store)

    service.delete_document("doc-1", reason="cleanup_request")

    # 业务删除已提交
    assert store.get_document("doc-1") is None
    # tombstone 使用事务内读到的删除前 revision（content_hash）
    rows = control_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["message_type"] == TARGET_DELETED_MESSAGE_TYPE
    payload = json.loads(row["payload_json"])
    assert payload == build_target_deleted_message(
        document_id="doc-1", document_revision="hash-1", reason="cleanup_request"
    )
    # 清理成功：cleanup_pending 复位；远端尚未 ACK：行保留
    assert row["cleanup_pending"] == 0
    assert row["remote_acked"] == 0
    # corpus dirty 同事务置位并递增 generation（indexed 保存已置 generation 1）
    state = store.read_corpus_state()
    assert state["dirty"] is True
    assert state["generation"] == 2


def test_delete_document_rolls_back_when_tombstone_registration_fails(store, monkeypatch):
    store.save_document(make_document(), make_content(), [])
    service = make_service(store)

    def broken_register(payload, control_key, connection=None, cleanup_pending=0):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(store, "register_control_message", broken_register)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        service.delete_document("doc-1")

    # SQLite 删除整体回滚：文档仍在，outbox 无残留，generation 停留在保存基线
    assert store.get_document("doc-1") is not None
    assert outbox_count(store) == 0
    assert store.read_corpus_state()["generation"] == 1


def test_delete_missing_document_is_rejected_idempotently(store):
    service = make_service(store)
    store.save_document(make_document(), make_content(), [])

    with pytest.raises(ValueError, match="document_not_found"):
        service.delete_document("missing")

    service.delete_document("doc-1")
    # 重复删除已删除文档：同样幂等报 document_not_found
    with pytest.raises(ValueError, match="document_not_found"):
        service.delete_document("doc-1")


def test_tombstone_registered_even_when_sample_outbox_full(store):
    # sample 容量打满（两行上限）
    store.register_outbox_message(sample_payload("evt-1"), max_rows=2, max_bytes=10**6)
    store.register_outbox_message(sample_payload("evt-2"), max_rows=2, max_bytes=10**6)
    with pytest.raises(Exception):
        store.register_outbox_message(sample_payload("evt-3"), max_rows=2, max_bytes=10**6)
    store.save_document(make_document(), make_content(), [])
    service = make_service(store)

    service.delete_document("doc-1")

    # 控制消息不受 sample 配额约束：满箱仍登记
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["message_type"] == TARGET_DELETED_MESSAGE_TYPE


def test_delete_document_performs_external_cleanup_after_commit(store):
    vector_index = FakeVectorIndex()
    object_store = FakeObjectStore()
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=vector_index, object_store=object_store)

    service.delete_document("doc-1")

    # SQLite 提交后清理外部资源：向量替换为空 + 对象删除
    assert vector_index.replaced == [("doc-1", [], [], {}, {})]
    assert object_store.deleted == ["doc-1"]


def test_tombstone_registered_even_when_capture_disabled(store):
    # capture 关闭只门控 EvaluationCapture 的 sample 捕获（T5/T6），删除路径
    # 无 capture 门控：关闭状态下 delete_document 仍登记 tombstone 并置脏
    capture = EvaluationCapture(store, enabled=False)
    RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=FakeVectorIndex(),
        document_repository=store,
        capture_hook=capture,
    )
    store.save_document(make_document(), make_content(), [])
    service = make_service(store)

    # 先证明关闭的 capture 确实不产生 sample（门控位于捕获层）
    capture.begin_attempt()
    capture.capture_success(
        context=None, query="q", filters=None, top_k=3, results=[]
    )
    with store._connect() as connection:
        sample_count = connection.execute(
            "SELECT COUNT(*) FROM evaluation_outbox WHERE message_type != ?",
            (TARGET_DELETED_MESSAGE_TYPE,),
        ).fetchone()[0]
    assert sample_count == 0

    service.delete_document("doc-1", reason="user_request")

    # tombstone 不受 capture 开关影响：仍登记且 corpus dirty 置位
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["message_type"] == TARGET_DELETED_MESSAGE_TYPE
    payload = json.loads(rows[0]["payload_json"])
    assert payload["document_id"] == "doc-1"
    assert payload["document_revision"] == "hash-1"
    assert payload["reason"] == "user_request"
    state = store.read_corpus_state()
    assert state["dirty"] is True


# ---------------------------------------------------------------------------
# 外部清理失败与恢复入口
# ---------------------------------------------------------------------------


def test_cleanup_failure_marks_pending_and_restart_scan_recovers(store):
    vector_index = FakeVectorIndex(fail=True)
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=vector_index)

    service.delete_document("doc-1")
    assert store.get_document("doc-1") is None
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["cleanup_pending"] == 1
    assert rows[0]["remote_acked"] == 0

    # 模拟重启恢复：新 service 实例 + 已修复的向量索引
    recovered_service = make_service(store, vector_index=FakeVectorIndex())
    resolved = recovered_service.retry_pending_cleanups()

    assert resolved == 1
    rows = control_rows(store)
    assert len(rows) == 1
    # 清理完成但远端未 ACK：控制行保留，等待投递确认
    assert rows[0]["cleanup_pending"] == 0
    assert rows[0]["remote_acked"] == 0


def test_scan_keeps_row_when_cleanup_done_but_not_remote_acked(store):
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=FakeVectorIndex(fail=True))
    service.delete_document("doc-1")
    service = make_service(store, vector_index=FakeVectorIndex())
    service.retry_pending_cleanups()

    # 再次扫描：无待清理状态，控制行不动
    resolved = service.retry_pending_cleanups()

    assert resolved == 0
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["cleanup_pending"] == 0
    assert rows[0]["remote_acked"] == 0


def test_scan_keeps_row_when_remote_acked_but_cleanup_still_pending(store):
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=FakeVectorIndex(fail=True))
    service.delete_document("doc-1")
    control_key = control_rows(store)[0]["control_key"]
    remote_ack_tombstone(store, control_key)

    # 远端已 ACK 但本地清理未完成：恢复入口重试清理仍失败，行保留
    still_broken = make_service(store, vector_index=FakeVectorIndex(fail=True))
    resolved = still_broken.retry_pending_cleanups()
    assert resolved == 0
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["cleanup_pending"] == 1
    assert rows[0]["remote_acked"] == 1

    # 清理修复后重试：remote_acked + 清理完成 -> 控制行删除
    recovered = make_service(store, vector_index=FakeVectorIndex())
    assert recovered.retry_pending_cleanups() == 1
    assert control_rows(store) == []


def test_scan_clears_row_when_remote_acked_and_cleanup_already_done(store):
    # 删除时清理成功（cleanup_pending=0），远端 ACK 在其后到达：
    # 恢复入口扫描补删控制行，避免 ACK 后行滞留
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=FakeVectorIndex())
    service.delete_document("doc-1")
    control_key = control_rows(store)[0]["control_key"]
    remote_ack_tombstone(store, control_key)

    assert service.retry_pending_cleanups() == 1
    assert control_rows(store) == []


def test_object_store_cleanup_failure_also_marks_pending(store):
    store.save_document(make_document(), make_content(), [])
    service = make_service(store, vector_index=FakeVectorIndex(), object_store=FakeObjectStore(fail=True))

    service.delete_document("doc-1")

    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["cleanup_pending"] == 1


def test_scan_continues_when_cleanup_status_writeback_is_busy(store, monkeypatch):
    # 状态回写走 50ms busy-timeout 连接，SQLITE_BUSY 是文档化预期：
    # 首行回写 busy 时扫描不得中止，跳过该行继续处理其余行（行自愈语义不变）
    store.save_document(
        make_document("doc-1", content_hash="hash-1"),
        make_content("doc-1", content_hash="hash-1"),
        [],
    )
    store.save_document(
        make_document("doc-2", content_hash="hash-2"),
        make_content("doc-2", content_hash="hash-2"),
        [],
    )
    service = make_service(store, vector_index=FakeVectorIndex(fail=True))
    service.delete_document("doc-1")
    service.delete_document("doc-2")

    original_mark = store.mark_control_cleanup_pending
    calls = {"count": 0}

    def busy_then_ok(document_id, document_revision, *, pending):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_mark(document_id, document_revision, pending=pending)

    monkeypatch.setattr(store, "mark_control_cleanup_pending", busy_then_ok)
    recovered = make_service(store, vector_index=FakeVectorIndex())

    resolved = recovered.retry_pending_cleanups()

    # 不抛出；busy 首行保持 pending 等下次恢复，其余行照常处理
    assert resolved == 1
    rows = {row["payload"]["document_id"]: row for row in store.list_control_messages()}
    assert rows["doc-1"]["cleanup_pending"] is True
    assert rows["doc-2"]["cleanup_pending"] is False


def test_delete_non_indexed_document_does_not_bump_corpus_generation(store):
    # 非 indexed 文档删除：indexed-only 快照未变，不置脏不递增 generation
    # （与 legacy 删除路径及 corpus snapshot 的 indexed-only 定义对齐）；
    # tombstone 控制行仍登记
    store.save_document(make_document(status=DocumentStatus.UPLOADED), make_content(), [])
    service = make_service(store)

    service.delete_document("doc-1")

    assert store.get_document("doc-1") is None
    assert store.read_corpus_state() is None
    rows = control_rows(store)
    assert len(rows) == 1
    assert rows[0]["message_type"] == TARGET_DELETED_MESSAGE_TYPE


# ---------------------------------------------------------------------------
# 外部残留不作为检索结果（T4 过滤仍然成立）
# ---------------------------------------------------------------------------


def test_external_residual_hits_are_not_returned_as_search_results(store):
    store.save_document(
        make_document("doc-keep", content_hash="hash-keep"),
        make_content("doc-keep", content_hash="hash-keep"),
        [],
    )
    store.save_document(
        make_document("doc-gone", content_hash="hash-gone"),
        make_content("doc-gone", content_hash="hash-gone"),
        [],
    )
    # Qdrant 残留：doc-gone 已被删除（SQLite 无行），向量索引仍返回命中
    vector_index = FakeVectorIndex()
    vector_index.results = [
        RetrievalResult(
            document_id="doc-keep",
            chunk_id="chunk-keep",
            score=0.9,
            snippet="kept",
            source={"kind": "upload", "uri": "keep.md"},
            tags={},
            metadata={},
        ),
        RetrievalResult(
            document_id="doc-gone",
            chunk_id="chunk-gone",
            score=0.8,
            snippet="stale residual",
            source={"kind": "upload", "uri": "gone.md"},
            tags={},
            metadata={},
        ),
    ]
    deletion_service = make_service(store, vector_index=vector_index)
    deletion_service.delete_document("doc-gone")
    assert store.get_document("doc-gone") is None

    retrieval_service = RetrievalService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=vector_index,
        document_repository=store,
    )

    results = retrieval_service.search(query="anything", top_k=5)

    # T4 过滤仍成立：外部残留命中不得成为有效检索结果
    assert [result.document_id for result in results] == ["doc-keep"]
    assert results[0].document_revision == "hash-keep"
