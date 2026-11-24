import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.evaluation import (
    PRODUCER_STATE_COUNT_KEYS,
    SAMPLE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    OutboxCapacityError,
    build_state_message,
    build_target_deleted_message,
    canonical_json_bytes,
)
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag


class SQLiteStore:
    # evaluation_corpus_state 单行 KV 的固定主键
    CORPUS_STATE_KEY = "corpus_state"

    # 捕获路径专用连接的短锁 busy timeout（秒）；业务连接保持默认 5s
    CAPTURE_BUSY_TIMEOUT_SECONDS = 0.05

    # 单条 IN 批量查询的参数上界（get_documents_by_ids 分批执行）
    BATCH_IDS_MAX = 10

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.cursor().executescript(
                """
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_display_name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_contents(
                    document_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    encoding TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_tags(
                    document_id TEXT NOT NULL,
                    tag_key TEXT NOT NULL,
                    tag_value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(document_id, tag_key, tag_value)
                );
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_jobs(
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE INDEX IF NOT EXISTS idx_document_tags_key_value ON document_tags(tag_key, tag_value);
                CREATE INDEX IF NOT EXISTS idx_chunks_document_ordinal ON chunks(document_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_index_jobs_document_created_at ON index_jobs(document_id, created_at);
                CREATE TABLE IF NOT EXISTS evaluation_outbox(
                    producer_event_id TEXT PRIMARY KEY,
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',   -- pending | dead_letter
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,                        -- UTC ISO；NULL=立即可领
                    last_error TEXT,
                    owner TEXT,                                -- 投递租约持有者
                    lease_until TEXT,                          -- 租约到期 UTC ISO
                    control_key TEXT,                          -- 控制消息确定性 PK 标记（tombstone 用）
                    cleanup_pending INTEGER NOT NULL DEFAULT 0,
                    remote_acked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_eval_outbox_status_retry ON evaluation_outbox(status, next_retry_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_outbox_control_key ON evaluation_outbox(control_key) WHERE control_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS evaluation_counters(
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    counter_epoch TEXT NOT NULL,
                    counters_json TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_corpus_state(
                    key TEXT PRIMARY KEY,
                    value TEXT,                                -- NULL=从未计算
                    dirty INTEGER NOT NULL DEFAULT 1,
                    generation INTEGER NOT NULL DEFAULT 0,
                    config_fingerprint TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _serialize_datetime(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> Document:
        return Document(
            id=row["id"],
            title=row["title"],
            source=DocumentSource(
                kind=row["source_kind"],
                uri=row["source_uri"],
                display_name=row["source_display_name"],
                metadata={},
            ),
            content_hash=row["content_hash"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
            status=DocumentStatus(row["status"]),
            created_at=SQLiteStore._parse_datetime(row["created_at"]),
            updated_at=SQLiteStore._parse_datetime(row["updated_at"]),
        )

    def save_document(self, document: Document, content: DocumentContent, tags: list[Tag]) -> None:
        tag_created_at = self._serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.cursor()
            previous = cursor.execute(
                "SELECT status, content_hash FROM documents WHERE id = ?", (document.id,)
            ).fetchone()
            cursor.execute(
                """
                INSERT OR REPLACE INTO documents(
                    id, title, source_kind, source_uri, source_display_name, content_hash,
                    content_type, size_bytes, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.title,
                    document.source.kind,
                    document.source.uri,
                    document.source.display_name,
                    document.content_hash,
                    document.content_type,
                    document.size_bytes,
                    document.status.value,
                    self._serialize_datetime(document.created_at),
                    self._serialize_datetime(document.updated_at),
                ),
            )
            cursor.execute(
                """
                INSERT OR REPLACE INTO document_contents(
                    document_id, text, encoding, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    content.document_id,
                    content.text,
                    content.encoding,
                    content.content_hash,
                    self._serialize_datetime(content.created_at),
                ),
            )
            cursor.execute("DELETE FROM document_tags WHERE document_id = ?", (document.id,))
            cursor.executemany(
                """
                INSERT INTO document_tags(document_id, tag_key, tag_value, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(document.id, tag.key, tag.value, tag_created_at) for tag in tags],
            )
            if self._corpus_membership_changed(previous, document):
                self._mark_corpus_dirty_in_transaction(cursor)

    def get_document(self, document_id: str) -> Document | None:
        with self._connect() as connection:
            row = connection.cursor().execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return self._document_from_row(row) if row else None

    def get_documents_by_ids(self, document_ids: list[str]) -> list[Document]:
        """按 ID 批量查询文档；单条 IN 语句最多 _BATCH_IDS_MAX 个参数。

        去重后按 _BATCH_IDS_MAX 分批，同一次 with 连接内顺序执行；缺失的
        ID 不出现在结果中。仓储故障直接上抛，不做静默降级。
        """
        unique_ids: list[str] = []
        seen: set[str] = set()
        for document_id in document_ids:
            if document_id and document_id not in seen:
                seen.add(document_id)
                unique_ids.append(document_id)
        if not unique_ids:
            return []
        documents: list[Document] = []
        with self._connect() as connection:
            for start in range(0, len(unique_ids), self.BATCH_IDS_MAX):
                chunk = unique_ids[start:start + self.BATCH_IDS_MAX]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.cursor().execute(
                    f"SELECT * FROM documents WHERE id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                documents.extend(self._document_from_row(row) for row in rows)
        return documents

    def find_by_content_hash(self, content_hash: str) -> Document | None:
        with self._connect() as connection:
            row = connection.cursor().execute(
                "SELECT * FROM documents WHERE content_hash = ? ORDER BY created_at LIMIT 1",
                (content_hash,),
            ).fetchone()
        return self._document_from_row(row) if row else None

    def get_content(self, document_id: str) -> DocumentContent | None:
        with self._connect() as connection:
            row = connection.cursor().execute(
                "SELECT * FROM document_contents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return DocumentContent(
            document_id=row["document_id"],
            text=row["text"],
            encoding=row["encoding"],
            content_hash=row["content_hash"],
            created_at=self._parse_datetime(row["created_at"]),
        )

    def list_documents(
        self,
        tags: dict[str, str] | None = None,
        status: DocumentStatus | None = None,
    ) -> list[Document]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if tags:
            for key, value in tags.items():
                clauses.append(
                    "EXISTS (SELECT 1 FROM document_tags dt "
                    "WHERE dt.document_id = documents.id AND dt.tag_key = ? AND dt.tag_value = ?)"
                )
                params.extend([key, value])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.cursor().execute(f"SELECT * FROM documents{where} ORDER BY created_at", params).fetchall()
        return [self._document_from_row(row) for row in rows]

    def get_tags(self, document_id: str) -> list[Tag]:
        with self._connect() as connection:
            rows = connection.cursor().execute(
                """
                SELECT tag_key, tag_value FROM document_tags
                WHERE document_id = ?
                ORDER BY tag_key, tag_value
                """,
                (document_id,),
            ).fetchall()
        return [Tag(row["tag_key"], row["tag_value"]) for row in rows]

    def update_status(self, document_id: str, status: DocumentStatus) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            previous = cursor.execute(
                "SELECT status FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            cursor.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, self._serialize_datetime(datetime.now(timezone.utc)), document_id),
            )
            if previous is not None and (previous["status"] == DocumentStatus.INDEXED.value) != (
                status == DocumentStatus.INDEXED
            ):
                self._mark_corpus_dirty_in_transaction(cursor)

    def delete_document(self, document_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            previous = cursor.execute(
                "SELECT status FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            self._delete_document_tables(cursor, document_id)
            if previous is not None and previous["status"] == DocumentStatus.INDEXED.value:
                self._mark_corpus_dirty_in_transaction(cursor)

    @staticmethod
    def _delete_document_tables(cursor: sqlite3.Cursor, document_id: str) -> None:
        """业务 5 表删除序列（tags/chunks/contents/index_jobs/documents），
        delete_document 与 delete_document_with_control 共享。"""
        cursor.execute("DELETE FROM document_tags WHERE document_id = ?", (document_id,))
        cursor.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        cursor.execute("DELETE FROM document_contents WHERE document_id = ?", (document_id,))
        cursor.execute("DELETE FROM index_jobs WHERE document_id = ?", (document_id,))
        cursor.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    def replace_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        created_at = self._serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.cursor().execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            connection.cursor().executemany(
                """
                INSERT INTO chunks(
                    id, document_id, ordinal, text, content_hash, token_count, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.text,
                        chunk.content_hash,
                        chunk.token_count,
                        json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
                        created_at,
                    )
                    for chunk in chunks
                ],
            )

    def list_chunks(self, document_id: str) -> list[Chunk]:
        with self._connect() as connection:
            rows = connection.cursor().execute(
                "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal",
                (document_id,),
            ).fetchall()
        return [
            Chunk(
                id=row["id"],
                document_id=row["document_id"],
                ordinal=row["ordinal"],
                text=row["text"],
                content_hash=row["content_hash"],
                token_count=row["token_count"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def create_job(self, job: IndexJob) -> None:
        with self._connect() as connection:
            connection.cursor().execute(
                """
                INSERT INTO index_jobs(id, document_id, status, stage, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.document_id,
                    job.status.value,
                    job.stage.value,
                    job.error,
                    self._serialize_datetime(job.created_at),
                    self._serialize_datetime(job.updated_at),
                ),
            )

    def get_job(self, job_id: str) -> IndexJob | None:
        with self._connect() as connection:
            row = connection.cursor().execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return IndexJob(
            id=row["id"],
            document_id=row["document_id"],
            status=IndexJobStatus(row["status"]),
            stage=IndexStage(row["stage"]),
            error=row["error"],
            created_at=self._parse_datetime(row["created_at"]),
            updated_at=self._parse_datetime(row["updated_at"]),
        )

    def update_job(
        self,
        job_id: str,
        status: IndexJobStatus,
        stage: IndexStage,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.cursor().execute(
                "UPDATE index_jobs SET status = ?, stage = ?, error = ?, updated_at = ? WHERE id = ?",
                (
                    status.value,
                    stage.value,
                    error,
                    self._serialize_datetime(datetime.now(timezone.utc)),
                    job_id,
                ),
            )

    def health(self) -> dict[str, object]:
        try:
            with self._connect() as connection:
                connection.cursor().execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return {"status": "error"}
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # 评估存储：outbox / 计数器心跳 / 语料状态
    # 业务连接（默认 5s busy timeout）承载文档 CRUD，以及 replay/purge、
    # read_counters、read_corpus_state、read_corpus_snapshot、
    # write_corpus_state 等低频读写；评估捕获与投递写入走下方专用短锁
    # 连接（50ms busy timeout）——争用时可能抛 sqlite3.OperationalError
    # （SQLITE_BUSY），上层捕获循环（T5/T7）必须处理该错误并重试。
    # ------------------------------------------------------------------

    def _capture_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path, timeout=self.CAPTURE_BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        # 显式事务管理：读-改-写必须包在 BEGIN IMMEDIATE 中才原子
        connection.isolation_level = None
        return connection

    @contextmanager
    def _capture_transaction(self):
        connection = self._capture_connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        """调用方时间统一规范化为 UTC ISO（微秒精度）：outbox 租约比较依赖
        ISO 字符串的字典序，非 UTC 偏移字符串会破坏比较正确性。"""
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _canonical_payload(payload: dict) -> tuple[str, int]:
        encoded = canonical_json_bytes(payload)
        return encoded.decode("utf-8"), len(encoded)

    @staticmethod
    def _corpus_membership_changed(previous: sqlite3.Row | None, document: Document) -> bool:
        """判断 indexed 成员集合或其 content_hash 是否被本次保存改变。"""
        new_is_indexed = document.status == DocumentStatus.INDEXED
        old_is_indexed = previous is not None and previous["status"] == DocumentStatus.INDEXED.value
        if new_is_indexed != old_is_indexed:
            return True
        return new_is_indexed and previous["content_hash"] != document.content_hash

    @staticmethod
    def _mark_corpus_dirty_in_transaction(connection: sqlite3.Connection) -> None:
        """在调用方已有事务内置脏并递增 generation（同一事务提交）。"""
        connection.execute(
            """
            INSERT INTO evaluation_corpus_state(key, value, dirty, generation, config_fingerprint, updated_at)
            VALUES (?, NULL, 1, 1, NULL, ?)
            ON CONFLICT(key) DO UPDATE SET dirty = 1, generation = generation + 1, updated_at = excluded.updated_at
            """,
            (SQLiteStore.CORPUS_STATE_KEY, SQLiteStore._utc_now_iso()),
        )

    @staticmethod
    def _validate_counter_delta(delta: dict) -> dict[str, int]:
        validated: dict[str, int] = {}
        for key in PRODUCER_STATE_COUNT_KEYS:
            value = delta.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"counter_delta[{key}] 必须是非负 int")
            validated[key] = value
        return validated

    @staticmethod
    def _accumulate_counters(connection: sqlite3.Connection, delta: dict[str, int], now: str) -> dict:
        """单事务内的计数器读-改-写：epoch 仅在首次初始化时生成，sequence 不动。"""
        row = connection.execute(
            "SELECT counter_epoch, counters_json FROM evaluation_counters WHERE id = 1"
        ).fetchone()
        if row is None:
            counter_epoch = datetime.now(timezone.utc).isoformat()
            counters = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}
        else:
            counter_epoch = row["counter_epoch"]
            counters = json.loads(row["counters_json"])
        for key in PRODUCER_STATE_COUNT_KEYS:
            counters[key] += delta.get(key, 0)
        counters_json = json.dumps(counters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO evaluation_counters(id, counter_epoch, counters_json, sequence, updated_at)
            VALUES (1, ?, ?, 0, ?)
            ON CONFLICT(id) DO UPDATE SET counters_json = excluded.counters_json, updated_at = excluded.updated_at
            """,
            (counter_epoch, counters_json, now),
        )
        return {"counter_epoch": counter_epoch, **counters}

    @staticmethod
    def _outbox_capacity_usage(connection: sqlite3.Connection) -> sqlite3.Row:
        """sample 类（pending + dead_letter）的容量占用；dead_letter 仍占额度。"""
        return connection.execute(
            "SELECT COUNT(*) AS rows, COALESCE(SUM(payload_bytes), 0) AS bytes FROM evaluation_outbox "
            "WHERE message_type = ? AND status IN ('pending', 'dead_letter')",
            (SAMPLE_MESSAGE_TYPE,),
        ).fetchone()

    @staticmethod
    def _insert_outbox_row(
        connection: sqlite3.Connection,
        *,
        producer_event_id: str,
        message_type: str,
        payload_json: str,
        payload_bytes: int,
        now: str,
        control_key: str | None = None,
        ignore_duplicate: bool = False,
        cleanup_pending: int = 0,
    ) -> None:
        """统一 outbox 插入：sample 与控制消息共用，控制消息以 control_key
        作为 producer_event_id 存储列并依赖部分唯一索引做 INSERT OR IGNORE 幂等。
        cleanup_pending 供删除 tombstone 出生即置 1（外部清理尚未执行），
        使 cleanup_pending=0 恒等于"本地清理已完成"。"""
        verb = "INSERT OR IGNORE INTO" if ignore_duplicate else "INSERT INTO"
        connection.execute(
            f"""
            {verb} evaluation_outbox(
                producer_event_id, message_type, payload_json, payload_bytes,
                status, attempts, next_retry_at, last_error, owner, lease_until,
                control_key, cleanup_pending, remote_acked, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, ?, 0, ?, ?)
            """,
            (
                producer_event_id,
                message_type,
                payload_json,
                payload_bytes,
                control_key,
                cleanup_pending,
                now,
                now,
            ),
        )

    # -- outbox 登记 ----------------------------------------------------

    def register_outbox_message(self, payload: dict, max_rows: int, max_bytes: int) -> None:
        payload_json, payload_bytes = self._canonical_payload(payload)
        now = self._utc_now_iso()
        with self._capture_transaction() as connection:
            usage = self._outbox_capacity_usage(connection)
            if usage["rows"] + 1 > max_rows or usage["bytes"] + payload_bytes > max_bytes:
                raise OutboxCapacityError("evaluation outbox 容量超限")
            self._insert_outbox_row(
                connection,
                producer_event_id=payload["producer_event_id"],
                message_type=payload["type"],
                payload_json=payload_json,
                payload_bytes=payload_bytes,
                now=now,
            )

    def register_sample_with_counter(
        self, payload: dict, counter_delta: dict, max_rows: int, max_bytes: int
    ) -> dict:
        payload_json, payload_bytes = self._canonical_payload(payload)
        validated = self._validate_counter_delta(counter_delta)
        now = self._utc_now_iso()
        rejected = False
        counters: dict = {}
        with self._capture_transaction() as connection:
            usage = self._outbox_capacity_usage(connection)
            if usage["rows"] + 1 > max_rows or usage["bytes"] + payload_bytes > max_bytes:
                # 容量拒绝：计数照记（eligible/selected/queue_full 同额），sample 不落库
                rejected = True
                queue_full_delta = dict.fromkeys(PRODUCER_STATE_COUNT_KEYS, 0)
                queue_full_delta["eligible_count"] = validated["eligible_count"]
                queue_full_delta["selected_count"] = validated["eligible_count"]
                queue_full_delta["queue_full_count"] = validated["eligible_count"]
                counters = self._accumulate_counters(connection, queue_full_delta, now)
            else:
                self._insert_outbox_row(
                    connection,
                    producer_event_id=payload["producer_event_id"],
                    message_type=payload["type"],
                    payload_json=payload_json,
                    payload_bytes=payload_bytes,
                    now=now,
                )
                counters = self._accumulate_counters(connection, validated, now)
        if rejected:
            raise OutboxCapacityError("evaluation outbox 容量超限")
        return counters

    def register_control_message(
        self,
        payload: dict,
        control_key: str,
        connection: sqlite3.Connection | None = None,
        cleanup_pending: int = 0,
    ) -> None:
        """登记控制消息（tombstone）：不受 sample 配额约束；control_key 为确定性 PK，
        producer_event_id 存储列复用 control_key，INSERT OR IGNORE 保证幂等。
        connection 参数供删除事务（T8 delete_document_with_control）复用调用方
        已开启的事务连接，禁止在该场景另开连接；None 时使用专用捕获连接。
        cleanup_pending 供删除事务出生即置 1（默认 0 保持既有调用行为）。"""
        payload_json, payload_bytes = self._canonical_payload(payload)
        now = self._utc_now_iso()

        def insert(target: sqlite3.Connection) -> None:
            self._insert_outbox_row(
                target,
                producer_event_id=control_key,
                message_type=payload["type"],
                payload_json=payload_json,
                payload_bytes=payload_bytes,
                now=now,
                control_key=control_key,
                ignore_duplicate=True,
                cleanup_pending=cleanup_pending,
            )

        if connection is not None:
            insert(connection)
            return
        with self._capture_transaction() as dedicated:
            insert(dedicated)

    # -- 删除 tombstone 同事务（T8） ---------------------------------------

    @staticmethod
    def _target_deleted_control_key(document_id: str, document_revision: str) -> str:
        """控制消息确定性 PK：message_type/document_id/document_revision 三元组
        的 canonical JSON sha256（spec 7.2），重复登记幂等依赖该确定性。"""
        identity = {
            "message_type": TARGET_DELETED_MESSAGE_TYPE,
            "document_id": document_id,
            "document_revision": document_revision,
        }
        return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()

    def delete_document_with_control(self, document_id: str, reason: str) -> Document:
        """单事务完成文档删除与 tombstone 登记（spec 7.2 硬约束）。

        事务内读取删除前文档并校验存在（缺失报 document_not_found），完成业务
        5 表删除 + tombstone 控制消息登记 + corpus dirty 置位；任一步失败整体
        回滚，禁止在事务外读取版本后无条件删除。tombstone 载荷由事务内读到的
        Document 构造（document_revision 取删除前 content_hash）。控制行出生即
        cleanup_pending=1（外部清理尚未执行），使 cleanup_pending=0 恒等于
        "本地清理已完成"；控制消息不受 sample 配额约束，登记失败即整体回滚。
        返回删除前 Document 供调用方执行提交后的外部清理。"""
        with self._connect() as connection:
            cursor = connection.cursor()
            previous = cursor.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if previous is None:
                raise ValueError("document_not_found")
            document = self._document_from_row(previous)
            self._delete_document_tables(cursor, document_id)
            payload = build_target_deleted_message(
                document_id=document_id,
                document_revision=document.content_hash,
                reason=reason,
            )
            self.register_control_message(
                payload,
                self._target_deleted_control_key(document_id, document.content_hash),
                connection=connection,
                cleanup_pending=1,
            )
            # corpus dirty 条件置位（仅被删文档为 INDEXED 时）：与 legacy
            # store.delete_document 及 corpus snapshot 的 indexed-only 定义
            # 对齐，避免非 indexed 删除引发虚假 generation 递增/CAS 冲突
            if previous["status"] == DocumentStatus.INDEXED.value:
                self._mark_corpus_dirty_in_transaction(cursor)
            return document

    def list_control_messages(self) -> list[dict]:
        """返回全部控制行（control_key 非空），供外部清理恢复入口扫描；
        cleanup_pending 与 remote_acked 分别持久化，二者都完成才允许删行。"""
        with self._capture_transaction() as connection:
            rows = connection.execute(
                "SELECT control_key, payload_json, cleanup_pending, remote_acked "
                "FROM evaluation_outbox WHERE control_key IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [
            {
                "control_key": row["control_key"],
                "payload": json.loads(row["payload_json"]),
                "cleanup_pending": bool(row["cleanup_pending"]),
                "remote_acked": bool(row["remote_acked"]),
            }
            for row in rows
        ]

    def mark_control_cleanup_pending(
        self, document_id: str, document_revision: str, *, pending: bool
    ) -> None:
        """按删除目标（document_id/document_revision）回写控制行 cleanup_pending；
        control_key 由存储侧确定性推导，调用方无需感知 PK 细节。"""
        control_key = self._target_deleted_control_key(document_id, document_revision)
        with self._capture_transaction() as connection:
            connection.execute(
                "UPDATE evaluation_outbox SET cleanup_pending = ?, updated_at = ? "
                "WHERE control_key = ?",
                (1 if pending else 0, self._utc_now_iso(), control_key),
            )

    # -- outbox 领取与投递回写 -------------------------------------------

    def claim_due_outbox(
        self,
        owner: str,
        lease_seconds: int,
        now: datetime,
        message_types: tuple[str, ...] = (SAMPLE_MESSAGE_TYPE, TARGET_DELETED_MESSAGE_TYPE),
    ) -> list[dict]:
        now_iso = self._utc_iso(now)
        lease_until_iso = self._utc_iso(now + timedelta(seconds=lease_seconds))
        placeholders = ", ".join("?" for _ in message_types)
        with self._capture_transaction() as connection:
            rows = connection.execute(
                f"""
                UPDATE evaluation_outbox
                SET owner = ?, lease_until = ?, updated_at = ?
                WHERE status = 'pending'
                  AND message_type IN ({placeholders})
                  AND (owner IS NULL OR lease_until <= ?)
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND (message_type != ? OR remote_acked = 0)
                RETURNING producer_event_id, message_type, payload_json, attempts
                """,
                (
                    owner,
                    lease_until_iso,
                    now_iso,
                    *message_types,
                    now_iso,
                    now_iso,
                    TARGET_DELETED_MESSAGE_TYPE,
                ),
            ).fetchall()
        return [
            {
                "producer_event_id": row["producer_event_id"],
                "message_type": row["message_type"],
                "payload": json.loads(row["payload_json"]),
                "attempts": row["attempts"],
                "owner": owner,
                "lease_until": lease_until_iso,
            }
            for row in rows
        ]

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
    ) -> None:
        """仅当 owner 与当前租约持有者一致且租约未过期时生效（过期持有者写入被拒绝）。"""
        now_iso = self._utc_now_iso()
        with self._capture_transaction() as connection:
            if delete:
                connection.execute(
                    "DELETE FROM evaluation_outbox "
                    "WHERE producer_event_id = ? AND owner = ? AND lease_until > ? AND status = 'pending'",
                    (producer_event_id, owner, now_iso),
                )
                return
            connection.execute(
                """
                UPDATE evaluation_outbox
                SET status = COALESCE(?, status), attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE producer_event_id = ? AND owner = ? AND lease_until > ? AND status = 'pending'
                """,
                (status, attempts, next_retry_at, last_error, now_iso, producer_event_id, owner, now_iso),
            )

    def mark_remote_acked(self, producer_event_id: str, *, owner: str, now: datetime) -> None:
        now_iso = self._utc_iso(now)
        with self._capture_transaction() as connection:
            connection.execute(
                "UPDATE evaluation_outbox SET remote_acked = 1, updated_at = ? "
                "WHERE producer_event_id = ? AND owner = ? AND lease_until > ?",
                (now_iso, producer_event_id, owner, now_iso),
            )

    def clear_control_message(self, control_key: str) -> None:
        """仅在 remote_acked 且无未完成清理标记时删除控制消息。"""
        with self._capture_transaction() as connection:
            connection.execute(
                "DELETE FROM evaluation_outbox WHERE control_key = ? AND remote_acked = 1 AND cleanup_pending = 0",
                (control_key,),
            )

    # -- dead_letter 运维操作（仅供操作面调用，投递循环永不触发） ------------

    def replay_dead_letter(self, producer_event_ids: list[str] | None) -> int:
        now = self._utc_now_iso()
        with self._connect() as connection:
            if producer_event_ids is None:
                cursor = connection.execute(
                    "UPDATE evaluation_outbox SET status = 'pending', attempts = 0, "
                    "next_retry_at = NULL, last_error = NULL, updated_at = ? WHERE status = 'dead_letter'",
                    (now,),
                )
                return cursor.rowcount
            if not producer_event_ids:
                return 0
            placeholders = ", ".join("?" for _ in producer_event_ids)
            cursor = connection.execute(
                "UPDATE evaluation_outbox SET status = 'pending', attempts = 0, "
                "next_retry_at = NULL, last_error = NULL, updated_at = ? "
                f"WHERE status = 'dead_letter' AND producer_event_id IN ({placeholders})",
                (now, *producer_event_ids),
            )
            return cursor.rowcount

    def purge_dead_letter(self, producer_event_ids: list[str] | None) -> int:
        with self._connect() as connection:
            if producer_event_ids is None:
                cursor = connection.execute(
                    "DELETE FROM evaluation_outbox WHERE status = 'dead_letter'"
                )
                return cursor.rowcount
            if not producer_event_ids:
                return 0
            placeholders = ", ".join("?" for _ in producer_event_ids)
            cursor = connection.execute(
                f"DELETE FROM evaluation_outbox WHERE status = 'dead_letter' AND producer_event_id IN ({placeholders})",
                producer_event_ids,
            )
            return cursor.rowcount

    # -- 计数器与心跳 ----------------------------------------------------

    def increment_counters(self, delta: dict) -> dict:
        validated = self._validate_counter_delta(delta)
        now = self._utc_now_iso()
        with self._capture_transaction() as connection:
            return self._accumulate_counters(connection, validated, now)

    def snapshot_heartbeat(self, observed_at: str) -> bytes:
        """读取五计数并递增持久 sequence，返回 canonical 心跳负载字节。

        sequence 仅由心跳递增；counter_epoch 仅在计数存储首次初始化时生成。
        """
        now = self._utc_now_iso()
        with self._capture_transaction() as connection:
            row = connection.execute(
                "SELECT counter_epoch, counters_json, sequence FROM evaluation_counters WHERE id = 1"
            ).fetchone()
            if row is None:
                counters = {key: 0 for key in PRODUCER_STATE_COUNT_KEYS}
                counter_epoch = datetime.now(timezone.utc).isoformat()
                sequence = 1
            else:
                counters = json.loads(row["counters_json"])
                counter_epoch = row["counter_epoch"]
                sequence = row["sequence"] + 1
            connection.execute(
                "INSERT INTO evaluation_counters(id, counter_epoch, counters_json, sequence, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET sequence = excluded.sequence, updated_at = excluded.updated_at",
                (
                    counter_epoch,
                    json.dumps(counters, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    sequence,
                    now,
                ),
            )
        message = build_state_message(
            counter_epoch=counter_epoch,
            sequence=sequence,
            counts=counters,
            observed_at=observed_at,
        )
        return canonical_json_bytes(message)

    def read_counters(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT counter_epoch, counters_json FROM evaluation_counters WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return {"counter_epoch": row["counter_epoch"], **json.loads(row["counters_json"])}

    # -- 语料状态 ---------------------------------------------------------

    def read_corpus_state(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value, dirty, generation, config_fingerprint, updated_at "
                "FROM evaluation_corpus_state WHERE key = ?",
                (self.CORPUS_STATE_KEY,),
            ).fetchone()
        if row is None:
            return None
        return {
            "value": json.loads(row["value"]) if row["value"] is not None else None,
            "dirty": bool(row["dirty"]),
            "generation": row["generation"],
            "config_fingerprint": row["config_fingerprint"],
            "updated_at": row["updated_at"],
        }

    def read_corpus_snapshot(self) -> tuple[dict[str, str], int]:
        """单次读事务返回 indexed 文档清单与当前 generation。

        显式 BEGIN 包住两条 SELECT，保证清单与 generation 原子成对；且 generation
        先读、清单后读：任何成员变化都会递增 generation，撕裂读只会配对
        新文档 + 旧 generation，T3 的 CAS 写入必然失败重试，不会发布过期修订。
        """
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                state_row = connection.execute(
                    "SELECT generation FROM evaluation_corpus_state WHERE key = ?",
                    (self.CORPUS_STATE_KEY,),
                ).fetchone()
                rows = connection.execute(
                    "SELECT id, content_hash FROM documents WHERE status = ? ORDER BY id",
                    (DocumentStatus.INDEXED.value,),
                ).fetchall()
            except BaseException:
                connection.rollback()
                raise
        return (
            {row["id"]: row["content_hash"] for row in rows},
            state_row["generation"] if state_row is not None else 0,
        )

    def write_corpus_state(self, value: dict, expected_generation: int, config_fingerprint: str | None) -> bool:
        """原子 CAS 写入：generation 匹配才写入并清除 dirty；失败返回 False 不改值。"""
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = self._utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE evaluation_corpus_state SET value = ?, dirty = 0, config_fingerprint = ?, updated_at = ? "
                "WHERE key = ? AND generation = ?",
                (value_json, config_fingerprint, now, self.CORPUS_STATE_KEY, expected_generation),
            )
            if cursor.rowcount == 1:
                return True
            if expected_generation != 0:
                return False
            try:
                connection.execute(
                    "INSERT INTO evaluation_corpus_state(key, value, dirty, generation, config_fingerprint, updated_at) "
                    "VALUES (?, ?, 0, 0, ?, ?)",
                    (self.CORPUS_STATE_KEY, value_json, config_fingerprint, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_corpus_dirty(self) -> int:
        """无共享事务的调用方置脏入口：递增 generation 并置 dirty，返回新 generation。

        RETURNING 在同一语句内取回 generation，不会读到其他写者随后递增的值。
        """
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO evaluation_corpus_state(key, value, dirty, generation, config_fingerprint, updated_at)
                VALUES (?, NULL, 1, 1, NULL, ?)
                ON CONFLICT(key) DO UPDATE SET dirty = 1, generation = generation + 1, updated_at = excluded.updated_at
                RETURNING generation
                """,
                (self.CORPUS_STATE_KEY, self._utc_now_iso()),
            ).fetchone()
        return row["generation"]

    def list_indexed_document_hashes(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, content_hash FROM documents WHERE status = ? ORDER BY id",
                (DocumentStatus.INDEXED.value,),
            ).fetchall()
        return {row["id"]: row["content_hash"] for row in rows}
