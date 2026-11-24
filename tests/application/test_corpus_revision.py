"""CorpusRevisionService 单元测试：revision 计算、缓存、CAS 与配置失效。"""
import pytest

from app.application.corpus_revision import (
    CorpusRevisionConfig,
    CorpusRevisionService,
    fingerprint_from_splitter,
)
from app.domain.document import DocumentStatus


class FakeCorpusStateStore:
    """模拟 SQLiteStore 语料状态语义：CAS 写入、dirty/generation 递增。

    read_corpus_snapshot 与 SQLiteStore 同口径：只返回 indexed 文档、按 id 排序、
    generation 先于清单确定（此处直接取当前值，撕裂读语义由 T2 测试覆盖）。
    """

    def __init__(self):
        self._docs = {}  # doc_id -> {"status": DocumentStatus, "hash": str}
        self._state = None
        self.snapshot_calls = 0

    def add_document(self, doc_id: str, status: DocumentStatus, content_hash: str) -> None:
        self._docs[doc_id] = {"status": status, "hash": content_hash}

    def remove_document(self, doc_id: str) -> None:
        self._docs.pop(doc_id, None)

    def read_corpus_state(self) -> dict | None:
        if self._state is None:
            return None
        state = dict(self._state)
        state["value"] = dict(self._state["value"]) if self._state["value"] is not None else None
        return state

    def read_corpus_snapshot(self) -> tuple[dict[str, str], int]:
        self.snapshot_calls += 1
        generation = self._state["generation"] if self._state is not None else 0
        docs = {
            doc_id: meta["hash"]
            for doc_id, meta in sorted(self._docs.items())
            if meta["status"] == DocumentStatus.INDEXED
        }
        return docs, generation

    def write_corpus_state(self, value: dict, expected_generation: int, config_fingerprint: str | None) -> bool:
        if self._state is None:
            if expected_generation != 0:
                return False
            self._state = {
                "value": value,
                "dirty": 0,
                "generation": 0,
                "config_fingerprint": config_fingerprint,
            }
            return True
        if self._state["generation"] != expected_generation:
            return False
        self._state = {
            **self._state,
            "value": value,
            "dirty": 0,
            "config_fingerprint": config_fingerprint,
        }
        return True

    def mark_corpus_dirty(self) -> int:
        if self._state is None:
            self._state = {"value": None, "dirty": 1, "generation": 1, "config_fingerprint": None}
        else:
            self._state = {
                **self._state,
                "dirty": 1,
                "generation": self._state["generation"] + 1,
            }
        return self._state["generation"]

    def list_indexed_document_hashes(self) -> dict[str, str]:
        return {
            doc_id: meta["hash"]
            for doc_id, meta in sorted(self._docs.items())
            if meta["status"] == DocumentStatus.INDEXED
        }


class FakeSplitter:
    chunk_size = 1000
    chunk_overlap = 100


@pytest.fixture
def store():
    return FakeCorpusStateStore()


@pytest.fixture
def config():
    return CorpusRevisionConfig(
        chunking_fingerprint="SimpleTextSplitter:1000:100",
        embedding_provider="fake-provider",
        embedding_model="fake-embed-1",
    )


@pytest.fixture
def service(store, config):
    return CorpusRevisionService(store, config)


def test_empty_corpus_has_deterministic_revision(service, store):
    revision = service.current()
    assert revision is not None
    assert len(revision) == 64
    int(revision, 16)  # sha256 hex

    # 另一套全新 store/config 重算同一空清单，得到同一 revision
    fresh = CorpusRevisionService(FakeCorpusStateStore(), CorpusRevisionConfig(
        chunking_fingerprint="SimpleTextSplitter:1000:100",
        embedding_provider="fake-provider",
        embedding_model="fake-embed-1",
    ))
    assert fresh.current() == revision


def test_indexed_membership_change_flips_revision(service, store):
    first = service.current()

    store.add_document("doc-1", DocumentStatus.INDEXED, "hash-a")
    store.mark_corpus_dirty()
    second = service.current()
    assert second is not None and second != first

    # 删除后清单回到空，revision 回到初始值（同内容重建/重复删除不承诺翻转）
    store.remove_document("doc-1")
    store.mark_corpus_dirty()
    assert service.current() == first


@pytest.mark.parametrize(
    "status",
    [DocumentStatus.UPLOADED, DocumentStatus.FAILED, DocumentStatus.DELETED, DocumentStatus.INDEXING],
)
def test_non_indexed_docs_do_not_affect_revision(service, store, status):
    store.add_document("doc-1", DocumentStatus.INDEXED, "hash-a")
    store.mark_corpus_dirty()
    baseline = service.current()

    # 非 indexed 文档加入、内容变化都不进入清单，revision 不翻转
    store.add_document("doc-2", status, "hash-b")
    store.mark_corpus_dirty()
    assert service.current() == baseline

    store.add_document("doc-2", status, "hash-b-changed")
    store.mark_corpus_dirty()
    assert service.current() == baseline


def test_dirty_cas_lost_race_returns_none(service, store):
    store.mark_corpus_dirty()             # generation 1
    gen = store.read_corpus_state()["generation"]
    # 模拟重算期间又一次写入
    store.mark_corpus_dirty()             # generation 2
    assert service._publish(revision="r", generation=gen) is None
    assert store.read_corpus_state()["dirty"] == 1


def test_current_cas_lost_race_via_public_path_returns_none(service, store):
    store.mark_corpus_dirty()             # generation 1

    original_snapshot = store.read_corpus_snapshot

    def racy_snapshot():
        # 模拟并发写：清单读取完成后、CAS 发布前 generation 被递增
        snapshot = original_snapshot()
        store.mark_corpus_dirty()         # generation 2
        return snapshot

    store.read_corpus_snapshot = racy_snapshot

    assert service.current() is None
    state = store.read_corpus_state()
    assert state["dirty"] == 1
    assert state["value"] is None


def test_config_fingerprint_change_forces_invalidation(store, config):
    service_a = CorpusRevisionService(store, config)
    first = service_a.current()
    state = store.read_corpus_state()
    assert state["dirty"] == 0
    assert state["config_fingerprint"] is not None

    # 零文档写入、不置脏：仅配置变化即强制失效并翻转 revision
    service_b = CorpusRevisionService(
        store,
        CorpusRevisionConfig(
            chunking_fingerprint=config.chunking_fingerprint,
            embedding_provider=config.embedding_provider,
            embedding_model="fake-embed-2",
        ),
    )
    second = service_b.current()
    assert second is not None and second != first
    assert store.read_corpus_state()["dirty"] == 0


def test_current_cache_hit_does_not_requery_documents(service, store):
    store.add_document("doc-1", DocumentStatus.INDEXED, "hash-a")
    store.mark_corpus_dirty()

    first = service.current()
    assert first is not None
    assert store.snapshot_calls == 1

    # 缓存命中：不 dirty 且指纹一致，不重查文档清单
    assert service.current() == first
    assert store.snapshot_calls == 1


def test_corrupt_cache_value_self_heals_via_recompute(service, store):
    store.add_document("doc-1", DocumentStatus.INDEXED, "hash-a")
    store.mark_corpus_dirty()
    revision = service.current()
    assert revision is not None

    # 缓存值损坏（缺 revision 键）：视为缓存未命中，重算得到同一 revision
    store._state["value"] = {"unrelated": True}
    assert service.current() == revision


def test_recompute_store_error_returns_none(service, store):
    store.add_document("doc-1", DocumentStatus.INDEXED, "hash-a")
    store.mark_corpus_dirty()

    def broken_snapshot():
        raise RuntimeError("store unavailable")

    store.read_corpus_snapshot = broken_snapshot
    assert service.current() is None


def test_mark_dirty_delegates_and_does_not_swallow(service, store):
    service.mark_dirty()
    state = store.read_corpus_state()
    assert state["dirty"] == 1
    assert state["generation"] == 1

    def broken_mark():
        raise RuntimeError("store unavailable")

    store.mark_corpus_dirty = broken_mark
    with pytest.raises(RuntimeError):
        service.mark_dirty()


def test_fingerprint_from_splitter_derives_from_instance():
    assert fingerprint_from_splitter(FakeSplitter()) == "FakeSplitter:1000:100"
    assert fingerprint_from_splitter(FakeSplitter()) == fingerprint_from_splitter(FakeSplitter())
