"""评估样本投影与入箱：实现 RetrievalService 的 capture hook 协议。

职责边界：每次检索终态（成功/空/失败）把 results 投影为 evaluation.sample.v1
样本（固定常量裁剪：前 10 条、snippet 4096 UTF-8 字节、results canonical 1MiB、
完整信封 2MiB），经 store 单事务登记 outbox 并提交计数 delta。三个 hook 方法
不向业务抛出异常；捕获路径不改变业务结果。

计数 delta 契约（与 T2 sqlite_store 对齐，测试已锁定）：
- 正常登记路径传 eligible+selected+captured：成功时按此 delta 原子提交；容量
  拒绝时 store 在拒绝事务内部按 delta 的 eligible_count 提交 eligible/selected/
  queue_full 并抛 OutboxCapacityError，捕获层不得重复计数；
- 跳过路径（corpus_revision 缺失、身份不完整、信封不可裁剪、序列化失败）与
  store 非容量故障（事务回滚）由捕获层经 increment_counters 补记 eligible+
  selected+capture_error；补记失败只记健康日志。
"""
from __future__ import annotations

import logging
import uuid

from app.domain.evaluation import (
    EvaluationMessageError,
    OutboxCapacityError,
    build_sample_message,
    canonical_json_bytes,
    compute_integrity,
)
from app.domain.retrieval import RetrievalFilter, RetrievalResult

# 固定常量，不依赖可变观测配置
SAMPLE_MAX_RESULTS = 10
SNIPPET_MAX_BYTES = 4096
RESULTS_MAX_BYTES = 1024 * 1024
ENVELOPE_MAX_BYTES = 2 * 1024 * 1024
OUTBOX_MAX_ROWS = 10000
OUTBOX_MAX_BYTES = 256 * 1024 * 1024

logger = logging.getLogger(__name__)

_DELTA_CAPTURED = {"eligible_count": 1, "selected_count": 1, "captured_count": 1}
_DELTA_CAPTURE_ERROR = {
    "eligible_count": 1,
    "selected_count": 1,
    "capture_error_count": 1,
}


def _truncate_utf8(text: str, limit: int) -> str:
    """UTF-8 安全截断：不切多字节字符中间，丢弃的尾部不足一个完整字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


class EvaluationCapture:
    """实现 retrieval_service 的 capture hook 协议（begin_attempt/capture_success/capture_failure）。"""

    def __init__(self, store, *, enabled: bool) -> None:
        self._store = store
        self._enabled = enabled

    # -- capture hook 协议 ------------------------------------------------

    def begin_attempt(self) -> None:
        """协议 hook：计数在终态以单份 delta 原子提交，此处无状态、不写库。"""
        return

    def capture_success(self, context, query, filters, top_k, results) -> None:
        """成功/空检索捕获：投影 results 并登记样本。"""
        if not self._enabled:
            return
        self._capture(context=context, query=query, filters=filters, top_k=top_k, results=results)

    def capture_failure(self, context, query, filters, top_k, exc) -> None:
        """失败检索捕获：空 results 样本，沿用检索开始读取的 context 身份。"""
        if not self._enabled:
            return
        self._capture(context=context, query=query, filters=filters, top_k=top_k, results=[])

    # -- 内部流程 ----------------------------------------------------------

    def _capture(self, *, context, query, filters, top_k, results) -> None:
        if context.corpus_revision is None:
            # 接收端要求 corpus_revision 存在且非空：跳过捕获，不产生注定 422 的消息
            self._record_capture_error()
            return
        try:
            projection = self._project(results)
            # original 衡量裁剪前的完整投影：keep 窗口之外（rank 11+）出现不可
            # 序列化值同样拒绝整个样本——这是刻意设计，不为保住前 10 条而放宽
            original = canonical_json_bytes({"results": projection})
            clipped, integrity = self._clip_projection(projection, original)
            message = build_sample_message(
                producer_event_id=str(uuid.uuid4()),
                retrieval_id=context.retrieval_id,
                captured_at=context.captured_at,
                query=query,
                top_k=top_k,
                filters=self._filters_projection(filters),
                results=clipped,
                corpus_revision=context.corpus_revision,
                trace_ref=context.trace_ref,
                integrity=integrity,
            )
            message = self._fit_envelope(message, original)
        except EvaluationMessageError:
            self._record_capture_error()
            return
        except Exception:
            # 协议要求三个 hook 方法不向业务抛出异常；意外故障按捕获失败计数
            # （EvaluationMessageError 消息只含类型名；traceback 不含负载正文）
            logger.warning(
                "evaluation capture: unexpected projection failure", exc_info=True
            )
            self._record_capture_error()
            return
        if message is None:
            # query/filters 等不可裁剪部分超限：跳过样本，不入箱、不篡改 query
            self._record_capture_error()
            return
        self._register(message)

    def _project(self, results: list[RetrievalResult]) -> list[dict]:
        """全部原始结果投影为样本条目形状（rank 保留原始位次，snippet 未裁剪）。

        拟保留结果（前 10 条窗口内）任一缺失身份键则整体失败：不能伪造版本或
        静默丢条使样本看起来完整。
        """
        projection = []
        for rank, result in enumerate(results, start=1):
            entry = {
                "document_id": result.document_id,
                "document_revision": result.document_revision,
                "chunk_id": result.chunk_id,
                "chunk_content_hash": result.chunk_content_hash,
                "rank": rank,
                "similarity_score": result.score,
                "snippet": result.snippet,
            }
            if rank <= SAMPLE_MAX_RESULTS and any(
                not entry[key]
                for key in (
                    "document_id",
                    "document_revision",
                    "chunk_id",
                    "chunk_content_hash",
                )
            ):
                raise EvaluationMessageError(f"results[{rank - 1}] 缺失身份键，跳过整个样本")
            projection.append(entry)
        return projection

    def _clip_projection(self, projection: list[dict], original: bytes) -> tuple[list[dict], dict]:
        """前 10 条窗口、snippet UTF-8 截断、results canonical 1MiB 从尾部减条。

        返回裁剪后条目与样本 integrity（original 衡量投影前全部条目，observed
        衡量裁剪后条目）；任何裁剪置 truncated/incomplete。
        """
        truncated = len(projection) > SAMPLE_MAX_RESULTS
        clipped = []
        for entry in projection[:SAMPLE_MAX_RESULTS]:
            snippet = _truncate_utf8(entry["snippet"], SNIPPET_MAX_BYTES)
            if snippet != entry["snippet"]:
                truncated = True
            clipped.append({**entry, "snippet": snippet})
        observed = canonical_json_bytes({"results": clipped})
        while len(observed) > RESULTS_MAX_BYTES and clipped:
            clipped = clipped[:-1]
            truncated = True
            observed = canonical_json_bytes({"results": clipped})
        return clipped, compute_integrity(
            original, observed, truncated=truncated, incomplete=truncated
        )

    def _filters_projection(self, filters: RetrievalFilter | None) -> dict:
        if filters is None:
            return {}
        return {
            "tags": dict(filters.tags),
            "source_kind": filters.source_kind,
            "document_status": filters.document_status,
            "min_score": filters.min_score,
        }

    def _fit_envelope(self, message: dict, original: bytes) -> dict | None:
        """完整信封 canonical 编码不超过 2MiB；超限从尾部减条并重算 integrity。

        results 清空后仍超限（query/filters 等不可裁剪部分超限）返回 None。
        """
        if len(canonical_json_bytes(message)) <= ENVELOPE_MAX_BYTES:
            return message
        while message["results"]:
            message["results"] = message["results"][:-1]
            observed = canonical_json_bytes({"results": message["results"]})
            message["integrity"] = compute_integrity(
                original, observed, truncated=True, incomplete=True
            )
            if len(canonical_json_bytes(message)) <= ENVELOPE_MAX_BYTES:
                return message
        return None

    def _register(self, message: dict) -> None:
        try:
            self._store.register_sample_with_counter(
                message, _DELTA_CAPTURED, OUTBOX_MAX_ROWS, OUTBOX_MAX_BYTES
            )
        except OutboxCapacityError:
            # store 已在拒绝事务内提交 eligible/selected/queue_full，此处不得重复计数
            return
        except Exception:
            # 计数与入箱事务回滚（如 50ms 锁竞争 sqlite3.OperationalError）：尽力补记
            self._record_capture_error()

    def _record_capture_error(self) -> None:
        """尽力补记 capture_error；补记失败只记健康日志，不抛出、不含正文。"""
        try:
            self._store.increment_counters(_DELTA_CAPTURE_ERROR)
        except Exception:
            logger.error("evaluation capture: capture_error bookkeeping failed")
