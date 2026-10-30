import logging
import time
from datetime import datetime, timezone
import uuid

from app.application import telemetry_semantics, telemetry_details
from app.domain.document import DocumentStatus
from app.domain.indexing import IndexJob, IndexJobStatus, IndexStage
from app.domain.ports import DocumentParser, EmbeddingProvider, TextSplitter, VectorIndex
from app.domain.repositories import ChunkRepository, DocumentRepository, IndexJobRepository

logger = logging.getLogger(__name__)

# 完成日志模板常量（与组合根 register_application_log_events 登记的
# fail-closed 事件注册表保持同一来源；精确模板匹配）。
INDEX_COMPLETED_EVENT = "nkb.index.completed"
INDEX_COMPLETED_TEMPLATES = (
    INDEX_COMPLETED_EVENT + " document_id=%s outcome=%s chunks=%d stages=%s duration_ms=%.3f",
    INDEX_COMPLETED_EVENT + " document_id=%s outcome=%s stages=%s duration_ms=%.3f",
)
_SUCCESS_TEMPLATE = INDEX_COMPLETED_TEMPLATES[0]
_TERMINAL_TEMPLATE = INDEX_COMPLETED_TEMPLATES[1]


def _elapsed_seconds(started: float) -> float:
    return max(0.0, time.monotonic() - started)


def _record_terminal(outcome: str, *, chunk_count=None) -> None:
    """业务终态恰好一次；观测动作失败静默隔离，不改变业务结果。"""
    try:
        telemetry_semantics.record_index(outcome, chunk_count=chunk_count)
    except Exception:
        pass


def _set_chunk_count(run_span, chunk_count: int) -> None:
    try:
        telemetry_semantics.set_index_chunk_count(run_span, chunk_count)
    except Exception:
        pass


def _log_completion(
    outcome: str,
    *,
    document_id: str,
    started: float,
    stage_results,
    chunk_count=None,
) -> None:
    """完成日志：安全阶段结果摘要、耗时与计数，不含文档正文/标签值。

    document_id 经 safe_id 投影（与 span 属性同一界）；成功与取消为 INFO
    （取消是预期终态）；error 为 WARNING，保证观测 handler 未装配
    （root 默认 WARNING）时失败终态仍然可见。
    """
    try:
        safe_document_id = telemetry_semantics.safe_id(document_id) or "unknown"
        summary = ",".join(
            f"{stage}:{stage_outcome}" for stage, stage_outcome in stage_results
        )
        duration_ms = round(_elapsed_seconds(started) * 1000, 3)
        if outcome == telemetry_semantics.OUTCOME_SUCCESS and chunk_count is not None:
            logger.info(
                _SUCCESS_TEMPLATE,
                safe_document_id,
                outcome,
                chunk_count,
                summary,
                duration_ms,
                extra={"http_facts": {"outcome": outcome, "chunk_count": chunk_count, "duration_ms": duration_ms}},
            )
        else:
            level = logging.WARNING if outcome == telemetry_semantics.OUTCOME_ERROR else logging.INFO
            logger.log(level, _TERMINAL_TEMPLATE, safe_document_id, outcome, summary, duration_ms, extra={"http_facts": {"outcome": outcome, "duration_ms": duration_ms}})
    except Exception:
        pass


class IngestionService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        chunk_repository: ChunkRepository,
        index_job_repository: IndexJobRepository,
        document_parser: DocumentParser,
        text_splitter: TextSplitter,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
    ) -> None:
        self._document_repository = document_repository
        self._chunk_repository = chunk_repository
        self._index_job_repository = index_job_repository
        self._document_parser = document_parser
        self._text_splitter = text_splitter
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index

    def index_document(self, document_id: str) -> IndexJob:
        started = time.monotonic()
        with telemetry_semantics.index_run_span(document_id) as run_span:
            telemetry_details.update_current_details(index_input={"document_id":document_id})
            stage_results: list[tuple[str, str]] = []
            try:
                final_job, chunk_count = self._run_index_pipeline(
                    document_id, stage_results
                )
            except BaseException as exc:
                outcome = telemetry_semantics.outcome_for_exception(exc)
                telemetry_details.update_current_details(error={"outcome":outcome,"type":telemetry_semantics.error_type_for_exception(exc)})
                # 业务终态恰好一次（不存在文档/早期失败/回滚失败/取消均在此
                # 计一次），且在操作 span 仍为 current 时记录。
                _record_terminal(outcome)
                _log_completion(
                    outcome,
                    document_id=document_id,
                    started=started,
                    stage_results=stage_results,
                )
                raise
            telemetry_details.update_current_details(index_result={"document_id":document_id,"job_id":final_job.id,"status":final_job.status.value,"chunk_count":chunk_count,"stage_results":stage_results})
            _set_chunk_count(run_span, chunk_count)
            _record_terminal(
                telemetry_semantics.OUTCOME_SUCCESS, chunk_count=chunk_count
            )
            _log_completion(
                telemetry_semantics.OUTCOME_SUCCESS,
                document_id=document_id,
                started=started,
                stage_results=stage_results,
                chunk_count=chunk_count,
            )
            return final_job

    def _run_index_pipeline(
        self, document_id: str, stage_results: list[tuple[str, str]]
    ) -> tuple[IndexJob, int]:
        document = self._document_repository.get_document(document_id)
        content = self._document_repository.get_content(document_id)
        if document is None or content is None:
            raise ValueError("document_not_found")

        now = datetime.now(timezone.utc)
        job = IndexJob(
            id=uuid.uuid4().hex,
            document_id=document_id,
            status=IndexJobStatus.PENDING,
            stage=IndexStage.CREATED,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._index_job_repository.create_job(job)
        telemetry_details.update_current_details(index_input={"document_id":document_id,"job_id":job.id,"size_bytes":document.size_bytes})

        try:
            self._document_repository.update_status(document_id, DocumentStatus.INDEXING)

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.PARSING)
            parsed_text = self._stage(
                "parse",
                stage_results,
                lambda: self._document_parser.parse(
                    document.title,
                    content.text.encode(content.encoding or "utf-8"),
                ),
                document_id=document_id, job_id=job.id,
            )

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.SPLITTING)
            chunks = self._stage(
                "split",
                stage_results,
                lambda: self._text_splitter.split(document_id, parsed_text),
                document_id=document_id, job_id=job.id,
            )

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.EMBEDDING)
            vectors = self._stage(
                "embed",
                stage_results,
                lambda: self._embed_and_validate(chunks),
                document_id=document_id, job_id=job.id,
            )

            self._update_job(job.id, IndexJobStatus.RUNNING, IndexStage.WRITING_VECTOR_INDEX)
            self._stage(
                "write",
                stage_results,
                lambda: self._write_vector_index(document_id, document, chunks, vectors),
                document_id=document_id, job_id=job.id,
            )

            self._document_repository.update_status(document_id, DocumentStatus.INDEXED)
            self._update_job(job.id, IndexJobStatus.SUCCEEDED, IndexStage.COMPLETED)
            final_job = self._index_job_repository.get_job(job.id)
        except Exception as exc:
            self._document_repository.update_status(document_id, DocumentStatus.FAILED)
            self._update_job(
                job.id,
                IndexJobStatus.FAILED,
                IndexStage.FAILED,
                self._safe_error_summary(exc),
            )
            raise

        if final_job is None:
            raise RuntimeError("index_job_not_found")
        return final_job, len(chunks)

    def _stage(self, stage: str, stage_results, action, **facts):
        """在 `nkb.index.{stage}` span 内执行真实阶段调用并记录安全结果。"""
        with telemetry_semantics.index_stage_span(stage):
            telemetry_details.update_current_details(index_input={"stage":stage,**facts})
            try:
                result = action()
                summary = {"stage":stage,"outcome":"success",**facts}
                if stage == "parse":
                    summary["text_characters"] = len(result)
                elif stage == "split":
                    summary.update(chunk_count=len(result),token_count=sum(chunk.token_count for chunk in result))
                elif stage == "embed":
                    summary.update(output_count=len(result),dimensions=sorted({vector.dimensions for vector in result}))
                telemetry_details.update_current_details(index_result=summary)
            except BaseException as exc:
                stage_results.append(
                    (stage, telemetry_semantics.outcome_for_exception(exc))
                )
                raise
        stage_results.append((stage, telemetry_semantics.OUTCOME_SUCCESS))
        return result

    def _embed_and_validate(self, chunks):
        vectors = self._embedding_provider.embed_chunks(chunks)
        self._validate_embedding_vectors(chunks, vectors)
        return vectors

    def _write_vector_index(self, document_id, document, chunks, vectors) -> None:
        tags = {tag.key: tag.value for tag in self._document_repository.get_tags(document_id)}
        source = {"kind": document.source.kind, "uri": document.source.uri}
        previous_chunks = self._chunk_repository.list_chunks(document_id)
        self._chunk_repository.replace_chunks(document_id, chunks)
        try:
            self._vector_index.replace_document(document_id, chunks, vectors, tags, source)
        except Exception as vector_exc:
            try:
                self._chunk_repository.replace_chunks(document_id, previous_chunks)
            except Exception as restore_exc:
                raise vector_exc from restore_exc
            raise

    def _validate_embedding_vectors(self, chunks, vectors) -> None:
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding_vector_mismatch")
        for chunk, vector in zip(chunks, vectors, strict=True):
            if vector.chunk_id != chunk.id:
                raise RuntimeError("embedding_vector_mismatch")

    def _safe_error_summary(self, exc: Exception) -> str:
        message = str(exc)
        stable_codes = {"document_not_found", "embedding_vector_mismatch"}
        if message in stable_codes:
            return message
        return exc.__class__.__name__

    def _update_job(
        self,
        job_id: str,
        status: IndexJobStatus,
        stage: IndexStage,
        error: str | None = None,
    ) -> None:
        self._index_job_repository.update_job(job_id, status, stage, error)
