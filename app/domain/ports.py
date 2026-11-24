from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector
from app.domain.evaluation import (
    SAMPLE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    OutboxCapacityError,
)
from app.domain.retrieval import RetrievalFilter, RetrievalResult


@dataclass(frozen=True)
class StoredObject:
    path: Path
    size_bytes: int
    content_hash: str
    content_type: str


class ObjectStore(Protocol):
    def save(self, document_id: str, filename: str, content: bytes) -> StoredObject: ...

    def read_text(self, document_id: str) -> str: ...

    def delete(self, document_id: str) -> None: ...


class DocumentParser(Protocol):
    def parse(self, filename: str, content: bytes) -> str: ...


class TextSplitter(Protocol):
    def split(self, document_id: str, text: str) -> list[Chunk]: ...


class EmbeddingProvider(Protocol):
    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddingVector]: ...

    def embed_query(self, query: str) -> list[float]: ...


class VectorIndex(Protocol):
    def replace_document(
        self,
        document_id: str,
        chunks: list[Chunk],
        vectors: list[EmbeddingVector],
        tags: dict[str, str],
        source: dict[str, str],
    ) -> None: ...

    def search(
        self,
        vector: list[float],
        filters: RetrievalFilter,
        top_k: int,
    ) -> list[RetrievalResult]: ...

    def health(self) -> dict[str, object]: ...


class DependencyHealth(Protocol):
    def health(self) -> dict[str, object]: ...


class EvaluationStore(Protocol):
    """评估 outbox 与计数器的持久化端口。

    实现为同步存储；容量超限抛出 OutboxCapacityError（在 app.domain.evaluation
    中唯一定义）。捕获类写入使用短锁专用连接，业务连接不受影响。
    """

    def register_outbox_message(self, payload: dict, max_rows: int, max_bytes: int) -> None: ...

    def register_sample_with_counter(
        self, payload: dict, counter_delta: dict, max_rows: int, max_bytes: int
    ) -> dict: ...

    def register_control_message(self, payload: dict, control_key: str, connection: object | None = None) -> None:
        """connection 为实现侧（SQLiteStore）已开启事务的调用方连接，供 T8 删除
        事务复用；Protocol 不引入 sqlite3 类型（Domain 禁止 sqlite3 依赖），故以
        object 标注。默认 None 时实现自开捕获连接。"""

    def claim_due_outbox(
        self,
        owner: str,
        lease_seconds: int,
        now: datetime,
        message_types: tuple[str, ...] = (SAMPLE_MESSAGE_TYPE, TARGET_DELETED_MESSAGE_TYPE),
    ) -> list[dict]: ...

    def record_delivery_attempt(
        self,
        producer_event_id: str,
        *,
        delete: bool,
        status: str | None,
        attempts: int,
        next_retry_at: str | None,
        last_error: str | None,
        owner: str,
    ) -> None: ...

    def mark_remote_acked(self, producer_event_id: str, *, owner: str, now: datetime) -> None: ...

    def clear_control_message(self, control_key: str) -> None: ...

    def replay_dead_letter(self, producer_event_ids: list[str] | None) -> int: ...

    def purge_dead_letter(self, producer_event_ids: list[str] | None) -> int: ...

    def increment_counters(self, delta: dict) -> dict: ...

    def snapshot_heartbeat(self, observed_at: str) -> bytes: ...

    def read_counters(self) -> dict | None: ...


class CorpusStateStore(Protocol):
    """语料状态单行 KV 端口：dirty/generation/config_fingerprint 与 CAS 写入。"""

    def read_corpus_state(self) -> dict | None: ...

    def read_corpus_snapshot(self) -> tuple[dict[str, str], int]: ...

    def write_corpus_state(
        self, value: dict, expected_generation: int, config_fingerprint: str | None
    ) -> bool: ...

    def mark_corpus_dirty(self) -> int: ...

    def list_indexed_document_hashes(self) -> dict[str, str]: ...
