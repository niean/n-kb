import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.application import telemetry_semantics, telemetry_details
from app.application.telemetry_payloads import retrieval_output
from app.domain.document import DocumentStatus
from app.domain.evaluation import canonical_json_bytes, compute_integrity
from app.domain.ports import EmbeddingProvider, VectorIndex
from app.domain.retrieval import RetrievalFilter, RetrievalResult

# Legacy registration aliases only; lifecycle owns all completion publication.
RETRIEVAL_COMPLETED_EVENT = "nkb.retrieval.completed"
RETRIEVAL_COMPLETED_TEMPLATES = (
    RETRIEVAL_COMPLETED_EVENT + " outcome=%s results=%d duration_ms=%.3f",
    RETRIEVAL_COMPLETED_EVENT + " outcome=%s duration_ms=%.3f",
)
_SUCCESS_TEMPLATE = RETRIEVAL_COMPLETED_TEMPLATES[0]
_TERMINAL_TEMPLATE = RETRIEVAL_COMPLETED_TEMPLATES[1]


def _record_terminal(outcome: str, *, result_count=None) -> None:
    """业务终态恰好一次；观测动作失败静默隔离，不改变业务结果。"""
    try:
        telemetry_semantics.record_retrieval(outcome, result_count=result_count)
    except Exception:
        pass


def _response_projection(results: list[RetrievalResult]) -> dict:
    """HTTP 层结果投影，字段与 interfaces/http/schemas.retrieval_result_to_response
    对 RetrievalResult 的取值方式一致：9 字段完整响应形状，metadata 同样过滤
    "vector" 键、document_revision/chunk_content_hash 直接取 domain 值；用于响应
    完整性计算（canonical 输入即实际响应载荷对应的 results 形状）。
    """
    return {
        "results": [
            {
                "document_id": result.document_id,
                "chunk_id": result.chunk_id,
                # float 强转与 pydantic 响应序列化一致，避免 int score 的字节漂移
                # （json.dumps(1) 为 "1"，pydantic 载荷为 "1.0"）。
                "score": float(result.score),
                "snippet": result.snippet,
                "source": result.source,
                "tags": result.tags,
                "metadata": {
                    key: value for key, value in result.metadata.items() if key != "vector"
                },
                "document_revision": result.document_revision,
                "chunk_content_hash": result.chunk_content_hash,
            }
            for result in results
        ]
    }


@dataclass(frozen=True)
class RetrievalContext:
    """单次检索的响应上下文事实（retrieval_id/captured_at/corpus_revision/trace_ref/integrity）。"""

    retrieval_id: str
    captured_at: str  # datetime.now(timezone.utc).isoformat()，显式 UTC 偏移
    corpus_revision: str | None
    trace_ref: dict | None  # {"trace_id": 32hex, "span_id": 16hex, "sampled": bool} 或 None
    integrity: dict  # 响应 results 投影的完整性事实


@dataclass(frozen=True)
class RetrievalOutcome:
    """检索结果与响应上下文的组合返回。"""

    results: list[RetrievalResult]
    context: RetrievalContext


class RetrievalService:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
        document_repository=None,
        corpus_revision=None,
        capture_hook=None,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index
        self._document_repository = document_repository
        self._corpus_revision = corpus_revision
        self._capture_hook = capture_hook

    def search(
        self,
        query: str,
        filters: RetrievalFilter | None = None,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        return self.search_with_context(query, filters, top_k).results

    def search_with_context(
        self,
        query: str,
        filters: RetrievalFilter | None = None,
        top_k: int = 5,
    ) -> RetrievalOutcome:
        with telemetry_semantics.retrieval_span() as span:
            context = self._build_context(span)
            self._begin_attempt()
            telemetry_details.update_current_details(
                retrieval_input={
                    "query": query,
                    "top_k": top_k,
                    "min_score": filters.min_score if filters else None,
                    "filters": (
                        {
                            "tags": filters.tags,
                            "source_kind": filters.source_kind,
                            "document_status": filters.document_status,
                        }
                        if filters
                        else {}
                    ),
                }
            )
            try:
                vector = self._embedding_provider.embed_query(query)
                results = self._vector_index.search(
                    vector, filters or RetrievalFilter(), top_k
                )
                results = self._attach_document_revisions(results)
            except BaseException as exc:
                outcome = telemetry_semantics.outcome_for_exception(exc)
                telemetry_details.update_current_details(
                    error={
                        "outcome": outcome,
                        "type": telemetry_semantics.error_type_for_exception(exc),
                    }
                )
                # 业务终态恰好一次，且在操作 span 仍为 current 时记录。
                _record_terminal(outcome)
                self._capture_failure(context, query, filters, top_k, exc)
                raise
            try:
                attrs = telemetry_semantics.retrieval_attrs(len(results), filters)
            except Exception:
                attrs = {}
            for key, value in attrs.items():
                telemetry_semantics.set_span_attribute(span, key, value)
            telemetry_details.update_current_details(
                retrieval_output=retrieval_output(results)
            )
            _record_terminal(
                telemetry_semantics.OUTCOME_SUCCESS,
                result_count=len(results),
            )
            succeeded_context = replace(
                context, integrity=self._response_integrity(results)
            )
            self._capture_success(succeeded_context, query, filters, top_k, results)
            return RetrievalOutcome(results=results, context=succeeded_context)

    def _build_context(self, span) -> RetrievalContext:
        """在操作 span 内构造响应上下文：uuid、tz 时间、语料修订与 trace 引用。

        integrity 以全零不完整占位起步：检索成功后回填真实投影；失败路径保留
        该占位，消费方不得把失败上下文的 integrity 当作响应证据读取。
        """
        return RetrievalContext(
            retrieval_id=str(uuid.uuid4()),
            captured_at=datetime.now(timezone.utc).isoformat(),
            corpus_revision=self._current_corpus_revision(),
            trace_ref=self._trace_ref(span),
            integrity=compute_integrity(b"", b"", truncated=False, incomplete=True),
        )

    def _current_corpus_revision(self) -> str | None:
        # 服务故障与服务报告 None（未版本化）在此刻意合并为 None：下游统一按
        # 跳过并计数处理，不区分来源。
        if self._corpus_revision is None:
            return None
        try:
            return self._corpus_revision.current()
        except Exception:
            return None

    def _trace_ref(self, span) -> dict | None:
        try:
            span_context = span.get_span_context()
        except Exception:
            return None
        if span_context is None or not span_context.is_valid:
            return None
        return {
            "trace_id": format(span_context.trace_id, "032x"),
            "span_id": format(span_context.span_id, "016x"),
            "sampled": bool(span_context.trace_flags.sampled),
        }

    def _response_integrity(self, results: list[RetrievalResult]) -> dict:
        """响应 results 投影（HTTP 层结果形状）的完整性事实；投影失败按未投影处理。"""
        try:
            original = canonical_json_bytes(_response_projection(results))
        except Exception:
            return compute_integrity(b"", b"", truncated=False, incomplete=True)
        return compute_integrity(original, original, truncated=False, incomplete=False)

    def _safe_call(self, action, *args) -> None:
        """捕获动作失败静默隔离，不改变业务结果（同 _record_terminal）。"""
        try:
            action(*args)
        except Exception:
            pass

    def _begin_attempt(self) -> None:
        """尝试标记；hook 缺省或故障时静默跳过，不改变业务结果。"""
        hook = self._capture_hook
        if hook is None:
            return
        self._safe_call(hook.begin_attempt)

    def _capture_success(self, context, query, filters, top_k, results) -> None:
        """成功捕获；hook 缺省或故障时静默跳过，不改变业务结果。"""
        hook = self._capture_hook
        if hook is None:
            return
        self._safe_call(hook.capture_success, context, query, filters, top_k, results)

    def _capture_failure(self, context, query, filters, top_k, exc) -> None:
        """失败捕获；hook 缺省或故障时静默跳过，不改变原异常与业务结果。"""
        hook = self._capture_hook
        if hook is None:
            return
        self._safe_call(hook.capture_failure, context, query, filters, top_k, exc)

    def _attach_document_revisions(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        """回填 document_revision（文档内容 hash），并显式过滤失效命中。

        命中文档按 ID 去重后一次批量查询仓储（仓储实现侧按安全上界分批），
        替代逐结果的 N 次 SELECT。仅 indexed 文档保留并回填 revision；仓储
        缺失或非 indexed 的命中直接过滤，不 append 残留。仓储故障直接上抛，
        不做静默降级。
        """
        if self._document_repository is None:
            return results
        documents = {}
        for document in self._document_repository.get_documents_by_ids(
            [result.document_id for result in results]
        ):
            documents[document.id] = document
        enriched = []
        for result in results:
            document = documents.get(result.document_id)
            if document is None or document.status != DocumentStatus.INDEXED:
                continue
            revision = document.content_hash
            if revision and revision != result.document_revision:
                result = replace(result, document_revision=revision)
            enriched.append(result)
        return enriched
