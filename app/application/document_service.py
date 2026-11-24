from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import sqlite3
import uuid

from app.application import telemetry_semantics, telemetry_details
from app.application.telemetry_payloads import safe_source
from app.domain.document import (
    Document,
    DocumentContent,
    DocumentSource,
    DocumentStatus,
)
from app.domain.evaluation import TARGET_DELETED_MESSAGE_TYPE
from app.domain.ports import ObjectStore, VectorIndex
from app.domain.repositories import ChunkRepository, DocumentRepository
from app.domain.tag import Tag, parse_tags

UPLOAD_SPAN_NAME = "document.upload"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadDocumentCommand:
    filename: str
    content: bytes
    source: str | None
    tags: str | None


class DocumentService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        object_store: ObjectStore,
        max_upload_bytes: int,
        vector_index: VectorIndex | None = None,
        allowed_extensions: set[str] | None = None,
        chunk_repository: ChunkRepository | None = None,
        evaluation_control_register=None,
    ) -> None:
        self._document_repository = document_repository
        self._chunk_repository = chunk_repository or document_repository
        self._object_store = object_store
        self._max_upload_bytes = max_upload_bytes
        self._vector_index = vector_index
        self._allowed_extensions = allowed_extensions or {".md", ".markdown", ".txt"}
        # 原子删除端口（duck type）：须提供 delete_document_with_control /
        # list_control_messages / mark_control_cleanup_pending / clear_control_message。
        # 为 None 仅为旧测试桩兼容；生产组合根必须注入（T9 接线），禁止配置
        # 关闭或凭据缺失时回退非原子删除。
        self._evaluation_control_register = evaluation_control_register

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    def upload_document(self, command: UploadDocumentCommand) -> Document:
        # 与 retrieval/index 共享同一 nkb.* 操作 span 生命周期（统一 tracer
        # 与 operation_context 发布），业务合同不变。
        with telemetry_semantics.operation_span(UPLOAD_SPAN_NAME) as span:
            telemetry_details.update_current_details(
                document_input={
                    "filename": Path(command.filename).name,
                    "size_bytes": len(command.content),
                    "file_type": Path(command.filename).suffix.lower(),
                    "source": safe_source(
                        {"kind": "upload", "uri": command.source or command.filename}
                    ),
                    "tags": command.tags,
                }
            )
            document = self._upload_document(command)
            telemetry_semantics.set_span_attribute(span, "nkb.document.id", document.id)
            telemetry_details.update_current_details(
                document_input={
                    "filename": Path(command.filename).name,
                    "size_bytes": len(command.content),
                    "file_type": Path(command.filename).suffix.lower(),
                    "source": safe_source(
                        {"kind": "upload", "uri": command.source or command.filename}
                    ),
                    "tags": command.tags,
                    "document_id": document.id,
                    "status": document.status.value,
                }
            )
            return document

    def _upload_document(self, command: UploadDocumentCommand) -> Document:
        try:
            self._validate_upload(command.filename, command.content)
            text = self._decode_content(command.content)
            tags = parse_tags(command.tags)
        except ValueError:
            telemetry_semantics.set_operation_result(telemetry_semantics.OUTCOME_DENIED)
            raise
        content_hash = hashlib.sha256(command.content).hexdigest()
        existing_document = self._document_repository.find_by_content_hash(content_hash)
        if existing_document is not None:
            return existing_document

        document_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        stored_object = self._object_store.save(
            document_id, command.filename, command.content
        )

        source = DocumentSource(
            kind="upload",
            uri=command.filename,
            display_name=command.source or command.filename,
            metadata={},
        )
        document = Document(
            id=document_id,
            title=command.filename,
            source=source,
            content_hash=content_hash,
            content_type=stored_object.content_type,
            size_bytes=len(command.content),
            status=DocumentStatus.UPLOADED,
            created_at=now,
            updated_at=now,
        )
        content = DocumentContent(
            document_id=document_id,
            text=text,
            content_hash=content_hash,
            encoding="utf-8",
            created_at=now,
        )
        self._document_repository.save_document(document, content, tags)
        return document

    def list_documents(
        self,
        tags: dict[str, str] | None = None,
        status: DocumentStatus | None = None,
    ) -> list[Document]:
        return self._document_repository.list_documents(tags=tags, status=status)

    def get_document(self, document_id: str) -> Document | None:
        return self._document_repository.get_document(document_id)

    def get_content(self, document_id: str) -> DocumentContent | None:
        return self._document_repository.get_content(document_id)

    def get_tags(self, document_id: str) -> list[Tag]:
        return self._document_repository.get_tags(document_id)

    def list_chunks(self, document_id: str):
        if self._document_repository.get_document(document_id) is None:
            raise ValueError("document_not_found")
        return self._chunk_repository.list_chunks(document_id)

    def delete_document(self, document_id: str, reason: str = "user_request") -> None:
        """删除文档并同事务登记删除 tombstone（evaluation.target-deleted.v1）。

        SQLite 单事务完成业务删除 + tombstone 登记 + corpus dirty（spec 7.2
        硬约束）；提交后再尽力清理外部资源（向量索引与对象存储），清理失败
        不回滚删除，控制行保持 cleanup_pending=1，由 retry_pending_cleanups
        在重启后恢复。evaluation_control_register 为 None 时保留旧的非原子
        删除路径，仅供旧测试桩兼容。"""
        if self._evaluation_control_register is None:
            self._delete_document_legacy(document_id)
            return
        document = self._evaluation_control_register.delete_document_with_control(
            document_id, reason
        )
        try:
            self._cleanup_external_resources(document.id)
        except Exception:
            # SQLite 已提交：外部清理失败不改写删除结果；控制行出生即
            # cleanup_pending=1，显式回写保持状态语义并触发恢复入口重试
            logger.warning(
                "document external cleanup deferred to recovery doc_id=%s", document.id
            )
            self._write_cleanup_status(document, pending=True)
            return
        self._write_cleanup_status(document, pending=False)

    def retry_pending_cleanups(self) -> int:
        """外部清理恢复入口：扫描控制行并重试未完成的本地外部清理。

        要求已注入 evaluation_control_register（组合根职责，T9 接线负责）；
        本入口不提供运行时守卫，未注入时调用即失败。

        - cleanup_pending=1：重试外部清理；失败保持 pending 下次再试；成功且
          remote_acked 时删除控制行（clear_control_message），未 ACK 时复位
          cleanup_pending 等待投递确认。
        - cleanup_pending=0 且 remote_acked：删除时清理已完成，远端 ACK 后到，
          补删控制行（clear_control_message 仅在双条件满足时删行）。
        - cleanup_pending=0 且未 ACK：等待投递确认，不做处理。

        仅处理 evaluation.target-deleted.v1 控制行（未来新控制类型不进入
        本入口）。状态回写走 50ms busy-timeout 连接，SQLITE_BUSY 属文档化
        预期：单行回写 busy 时跳过该行继续扫描，行自愈语义不变。

        返回本次进入"清理完成"状态的控制行数（含已删行）。T9 lifespan 负责
        启动期与周期调用，本入口不依赖 lifespan，可直接测试。"""
        resolved = 0
        for row in self._evaluation_control_register.list_control_messages():
            payload = row["payload"]
            if payload.get("type") != TARGET_DELETED_MESSAGE_TYPE:
                continue
            document_id = payload["document_id"]
            document_revision = payload["document_revision"]
            if not row["cleanup_pending"]:
                if row["remote_acked"]:
                    try:
                        self._evaluation_control_register.clear_control_message(
                            row["control_key"]
                        )
                    except sqlite3.OperationalError:
                        # 回写 busy：跳过该行下次再试，不中止扫描
                        logger.warning(
                            "control message clear busy control_key=%s", row["control_key"]
                        )
                        continue
                    resolved += 1
                continue
            try:
                self._cleanup_external_resources(document_id)
            except Exception:
                # 保持 cleanup_pending=1，下次恢复再试；外部清理最终一致
                logger.warning(
                    "document external cleanup retry failed doc_id=%s", document_id
                )
                continue
            try:
                # 先复位 cleanup_pending 再判断删行：clear_control_message 仅在
                # remote_acked=1 且 cleanup_pending=0 双条件满足时删除
                self._evaluation_control_register.mark_control_cleanup_pending(
                    document_id, document_revision, pending=False
                )
                if row["remote_acked"]:
                    self._evaluation_control_register.clear_control_message(row["control_key"])
            except sqlite3.OperationalError:
                # 回写 busy：跳过该行下次再试（清理幂等，重扫无害），不中止扫描
                logger.warning(
                    "cleanup status writeback busy doc_id=%s", document_id
                )
                continue
            resolved += 1
        return resolved

    def _cleanup_external_resources(self, document_id: str) -> None:
        """SQLite 提交后的跨存储外部清理；任一失败抛出由调用方按 best-effort 处理。"""
        if self._vector_index is not None:
            self._vector_index.replace_document(document_id, [], [], tags={}, source={})
        self._object_store.delete(document_id)

    def _write_cleanup_status(self, document: Document, *, pending: bool) -> None:
        """清理状态回写控制行；SQLite 争用等存储故障按 best-effort 静默
        （恢复入口按 cleanup_pending 兜底），不把删除成功变成调用方错误。"""
        try:
            self._evaluation_control_register.mark_control_cleanup_pending(
                document.id, document.content_hash, pending=pending
            )
        except sqlite3.Error:
            logger.warning(
                "document cleanup status writeback failed doc_id=%s pending=%s",
                document.id,
                pending,
            )

    def _delete_document_legacy(self, document_id: str) -> None:
        """旧的非原子删除路径：仅供未注入 evaluation_control_register 的
        旧测试桩兼容；生产路径禁止使用。"""
        document = self._document_repository.get_document(document_id)
        if document is None:
            raise ValueError("document_not_found")
        if self._vector_index is not None:
            self._vector_index.replace_document(document_id, [], [], tags={}, source={})
        self._object_store.delete(document_id)
        self._document_repository.delete_document(document_id)

    def _validate_upload(self, filename: str, content: bytes) -> None:
        if Path(filename).suffix.lower() not in self._allowed_extensions:
            raise ValueError("unsupported_file_type")
        if len(content) > self._max_upload_bytes:
            raise ValueError("file_too_large")

    @staticmethod
    def _decode_content(content: bytes) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("unsupported_file_type") from exc
