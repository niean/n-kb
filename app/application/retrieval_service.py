from app.application import telemetry_semantics, telemetry_details
from app.application.telemetry_payloads import retrieval_output
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


class RetrievalService:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._vector_index = vector_index

    def search(
        self,
        query: str,
        filters: RetrievalFilter | None = None,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        with telemetry_semantics.retrieval_span() as span:
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
            return results
