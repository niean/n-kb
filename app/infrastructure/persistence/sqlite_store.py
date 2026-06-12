import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.chunk import Chunk
from app.domain.document import Document, DocumentContent, DocumentSource, DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.tag import Tag


class SQLiteStore:
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
            connection.executescript(
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
            connection.execute(
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
            connection.execute(
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
            connection.execute("DELETE FROM document_tags WHERE document_id = ?", (document.id,))
            connection.executemany(
                """
                INSERT INTO document_tags(document_id, tag_key, tag_value, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(document.id, tag.key, tag.value, tag_created_at) for tag in tags],
            )

    def get_document(self, document_id: str) -> Document | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return self._document_from_row(row) if row else None

    def get_content(self, document_id: str) -> DocumentContent | None:
        with self._connect() as connection:
            row = connection.execute(
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
            rows = connection.execute(f"SELECT * FROM documents{where} ORDER BY created_at", params).fetchall()
        return [self._document_from_row(row) for row in rows]

    def get_tags(self, document_id: str) -> list[Tag]:
        with self._connect() as connection:
            rows = connection.execute(
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
            connection.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, self._serialize_datetime(datetime.now(timezone.utc)), document_id),
            )

    def delete_document(self, document_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM document_tags WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM document_contents WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM index_jobs WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    def replace_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        created_at = self._serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            connection.executemany(
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
            rows = connection.execute(
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
            connection.execute(
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
            row = connection.execute("SELECT * FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
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
            connection.execute(
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
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return {"status": "error"}
        return {"status": "ok"}
