import sys
import threading
from dataclasses import dataclass
from typing import Any, Optional

from app.application.document_service import DocumentService
from app.application.health_service import HealthService
from app.application.ingestion_service import IngestionService
from app.application.retrieval_service import RetrievalService
from app.config import Settings
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider
from app.infrastructure.parsers.text_parser import SimpleTextParser, SimpleTextSplitter
from app.infrastructure.persistence.sqlite_store import SQLiteStore
from app.infrastructure.storage.local_object_store import LocalObjectStore
from app.infrastructure.vector.qdrant_index import QdrantVectorIndex


class _UnavailableQdrantClient:
    def get_collections(self):
        raise RuntimeError("qdrant_client_not_installed")


@dataclass(frozen=True)
class AppServices:
    documents: DocumentService
    ingestion: IngestionService
    index_jobs: SQLiteStore
    retrieval: RetrievalService
    health: HealthService


def _build_vector_index(settings: Settings) -> QdrantVectorIndex:
    try:
        return QdrantVectorIndex(
            qdrant_url=settings.qdrant_url,
            collection_name=settings.qdrant_collection,
        )
    except RuntimeError as exc:
        if str(exc) != "qdrant_client_not_installed":
            raise
        return QdrantVectorIndex(
            qdrant_url=settings.qdrant_url,
            collection_name=settings.qdrant_collection,
            client=_UnavailableQdrantClient(),
        )


def build_services(
    settings: Settings,
    *,
    embedding_observer: Any = None,
) -> AppServices:
    """组装业务服务。

    ``embedding_observer`` 是组合根注入的可选观测 scope 工厂（Application
    层 helper；Infrastructure 不反向 import Application）。缺省 None 保持
    旧调用能力（无观测）。
    """
    sqlite_store = SQLiteStore(settings.sqlite_path)
    object_store = LocalObjectStore(settings.storage_root)
    parser = SimpleTextParser()
    splitter = SimpleTextSplitter()
    embedding_provider = OllamaEmbeddingProvider(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        embedding_observer=embedding_observer,
    )
    vector_index = _build_vector_index(settings)

    document_service = DocumentService(
        document_repository=sqlite_store,
        object_store=object_store,
        vector_index=vector_index,
        max_upload_bytes=settings.max_upload_bytes,
        allowed_extensions=settings.allowed_file_extensions,
    )
    ingestion_service = IngestionService(
        document_repository=sqlite_store,
        chunk_repository=sqlite_store,
        index_job_repository=sqlite_store,
        document_parser=parser,
        text_splitter=splitter,
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
    retrieval_service = RetrievalService(
        embedding_provider=embedding_provider,
        vector_index=vector_index,
    )
    health_service = HealthService(
        sqlite=sqlite_store,
        qdrant=vector_index,
        ollama=embedding_provider,
    )
    return AppServices(
        documents=document_service,
        ingestion=ingestion_service,
        index_jobs=sqlite_store,
        retrieval=retrieval_service,
        health=health_service,
    )


# ---------------------------------------------------------------------------
# 组合根观测装配（HTTP 与 stdio 两入口复用）
# ---------------------------------------------------------------------------


def make_embedding_cost_calculator(settings: Settings) -> Any:
    """构造 embeddings 计价闭包（组合根持有受信身份与定价目录）。

    计价身份来自服务端受信配置（billing_provider 固定 ollama，billing_model
    取 settings.embedding_model 原始名），provider 适配器、请求 options 与
    响应模型不能覆盖。目录在启动期加载一次：未配置或失效时闭包仍可用，
    resolve 内部降级为 unavailable，成本按 pricing_unavailable 处理；无法
    从文本长度/chunk 数/向量维度估算 Token。输入是 Application 层承接的
    协议事实 dict（input_state: known/invalid/unknown），输出是观测结果
    dict（estimated 携带金额分项，unknown 携带八类原因之一），形态与
    Domain AttemptCost 契约一致。
    """
    from app.domain.cost import (
        AttemptUsage,
        TokenValue,
        calculate_attempt_cost,
        canonical_decimal_string,
    )
    from app.infrastructure.embedding.pricing_catalog import JsonPricingCatalog

    pricing_catalog = JsonPricingCatalog.load(
        settings.pricing_catalog_path, settings.pricing_catalog_expected_sha256
    )
    identity = ("ollama", settings.embedding_model)

    def _result(
        *,
        status,
        reason=None,
        pricing_version=None,
        price_id=None,
        amount_usd=0.0,
        cost_decimal=None,
        components=None,
    ) -> dict:
        return {
            "status": status,
            "amount_usd": amount_usd,
            "pricing_version": pricing_version,
            "price_id": price_id,
            "cost_decimal": cost_decimal,
            "components": components,
            "currency": "USD",
            "reason": reason,
            "billing_provider": identity[0],
            "billing_model": identity[1],
        }

    def calculate(facts: Any) -> Any:
        if not isinstance(facts, dict):
            return None
        state = facts.get("input_state")
        value = facts.get("input_value")
        # embeddings 无 output/cache 桶概念，声明 not_applicable（不输出
        # 虚构 0 Token）；input 桶按协议事实承接：缺失 unknown，存在但
        # 非法原样携带（Domain 计算器判 invalid_usage）。
        if state == "known" and type(value) is int and value >= 0:
            input_value = TokenValue.known(value)
        elif state == "invalid":
            input_value = TokenValue.known(value if value is not None else True)
        else:
            input_value = TokenValue.unknown()
        usage = AttemptUsage(
            buckets={
                "input": input_value,
                "output": TokenValue.not_applicable(),
                "cache_read": TokenValue.not_applicable(),
                "cache_write": TokenValue.not_applicable(),
            }
        )
        resolution = pricing_catalog.resolve(identity, "embeddings")
        cost = calculate_attempt_cost(usage, identity, resolution)
        if cost.status != "estimated":
            # 目录加载成功的 unknown 保留版本；目录失效（版本 None）省略。
            # 唯一命中价格项后的 unknown 保留 price_id（spec：追溯要求），
            # 未定位价格项的 unknown 该值为 None（组合根不携带即不投影）。
            return _result(
                status="unknown",
                reason=cost.reason.value,
                pricing_version=cost.pricing_version or pricing_catalog.pricing_version,
                price_id=cost.price_id,
            )
        return _result(
            status="estimated",
            amount_usd=float(cost.amount_usd),
            pricing_version=cost.pricing_version,
            price_id=cost.price_id,
            cost_decimal=canonical_decimal_string(cost.amount_usd),
            components={bucket: float(item) for bucket, item in cost.components.items()},
        )

    return calculate


def _configure_embedding_cost_calculator(settings: Settings, handle: Any) -> None:
    """观测启用时装配计价闭包；停机回调清除，避免跨实例泄漏。"""
    import app.application.telemetry_semantics as telemetry_semantics

    try:
        telemetry_semantics.configure_embedding_cost_calculator(
            make_embedding_cost_calculator(settings)
        )
        if handle is not None:
            handle.register_stop_callback(
                lambda: telemetry_semantics.configure_embedding_cost_calculator()
            )
    except Exception:
        # 计价装配失败只降级观测（成本事实缺席），不阻断业务启动
        telemetry_semantics.configure_embedding_cost_calculator()


def _start_profiler(handle: Any, settings: Settings) -> None:
    """创建并启动 Profile 采样器（缺 py-spy 是有界 degraded，不是失败）。

    顺序遵循 T6 合同：以 handle.profiler_on_status() 构造，先注册 stop
    callback，再 start；start 成功才 mark_profiler_attached。"""
    from app.infrastructure.observability.profiling import get_or_create_profiler

    try:
        sampler = get_or_create_profiler(
            settings,
            instance_id=handle.instance_id,
            on_status=handle.profiler_on_status(),
        )
    except Exception:
        return
    if sampler is None:
        return
    if not sampler.register_with_handle(handle):
        # 句柄已 shutdown：不再启动无法被回收的采样线程。
        return
    if sampler.start():
        handle.mark_profiler_attached()


def register_application_log_events() -> None:
    """登记固定契约的完成事件，不再注册内部兼容日志模板。"""
    from app.infrastructure.observability.logging import register_safe_event

    from app.application import telemetry_contract
    import logging
    logging.getLogger("app.application").setLevel(logging.INFO)
    for event in telemetry_contract.EVENTS:
        register_safe_event(event, event)


# 只读自检快照的固定信号集合（与 setup.py 的四信号一致）。
_OBSERVATION_SIGNALS = ("traces", "metrics", "logs", "profile")


def get_suppress_client_spans() -> Any:
    """解析 Infrastructure 层 client span 抑制实现（函数内延迟 import）。

    stdio 入口等非组合根模块经此函数取得实现，避免直接 import
    app.infrastructure 符号；main.py 等组合根仍可直接 import。
    """
    from app.infrastructure.observability.instrumentors import suppress_client_spans

    return suppress_client_spans

_OBSERVATION_PAGE_SIZE = 10


def _disabled_signal() -> dict:
    return {
        "status": "disabled",
        "delivery": "disabled",
        "attempt_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "last_success": None,
        "last_failure": None,
        "current_error": None,
    }


def build_observation_snapshot(settings: Settings, handle: Any) -> Any:
    """构造只读自检快照 callable（T9 合同，注入 ``app.state``）。

    Interfaces 只消费该 callable（``observation_snapshot(page, size) ->
    dict``），不接触 runtime、Infrastructure 类型或任何 SDK。本函数闭包
    组合根已持有的依赖：观测句柄（``None`` = 禁用）、配置中已验证的
    Grafana 外链 base URL（仅非空时输出）与 Application 层 semconv 版本。

    - ``handle=None``：返回完整合同的 disabled 快照（零观测装配，不是
      错误）；``handle`` 为诊断句柄（``diagnostic_error`` 非空）时透传
      其 degraded 状态，不误报 disabled。
    - 分页只作用于 ring buffer spans（最近 100 条已结束 sampled span）。
    - 纯投影：无完整配置、无凭据、无原异常，不发起网络探测。
    """
    from app.application import telemetry_semantics

    from app.interfaces.http.observation_links import build_links, trace_link, valid_url

    grafana = {
        "base_url": settings.otel_grafana_base_url if valid_url(settings.otel_grafana_base_url) else None,
        "traces_base_url": settings.otel_grafana_traces_base_url if valid_url(settings.otel_grafana_traces_base_url) else None,
    }
    semconv_version = telemetry_semantics.SEMCONV_VERSION

    def observation_snapshot(page: int = 1, size: int = _OBSERVATION_PAGE_SIZE) -> dict:
        def _base_payload(
            status: str, mode: str, diagnostic_error: Optional[str]
        ) -> dict:
            """disabled/兜底分支共用的合同底座，避免两处手写形状漂移。"""
            return {
                "status": status,
                "mode": mode,
                "instance_id": None,
                "sample_ratio": settings.otel_traces_sample_ratio,
                "semconv_version": semconv_version,
                "signals": {name: _disabled_signal() for name in _OBSERVATION_SIGNALS},
                "spans": [],
                "pagination": {"page": page, "size": size, "total": 0, "pages": 0},
                "grafana": dict(grafana),
                "links": build_links(settings),
                "metric_snapshot": {"status": status, "instruments": [], "error": diagnostic_error, "collected_at": None, "stale": False, "collection_count": 0, "collection_interval_millis": 15000, "last_success_at": None, "truncated": False, "truncation_scope": []},
                "logs": {"status": status, "items": [], "retained_count": 0, "capacity": 100, "returned_count": 0, "evicted_count": 0},
                "dropped_after_deadline": 0,
                "diagnostic_error": diagnostic_error,
                "differing_fields": [],
            }

        if handle is None:
            payload = _base_payload("disabled", "disabled", None)
            payload["pagination"] = {
                "page": page, "size": size, "total": 0, "pages": 0,
            }
            return payload
        try:
            snapshot = handle.snapshot()
        except Exception:
            # T4 合同 snapshot() 不抛出；防御性兜底仍给出可读 degraded，
            # 根因写一行有界 stderr 诊断（不输出原异常文本）。
            sys.stderr.write(
                "n-kb observability: snapshot() failed; reporting degraded\n"
            )
            return _base_payload("degraded", "unknown", "snapshot_failed")
        spans = list(snapshot.get("spans") or [])
        total = len(spans)
        start = (page - 1) * size
        snapshot["spans"] = spans[start : start + size]
        snapshot["pagination"] = {
            "page": page,
            "size": size,
            "total": total,
            "pages": (total + size - 1) // size,
        }
        snapshot["grafana"] = dict(grafana)
        snapshot.setdefault("metric_snapshot", {"status": "unknown", "instruments": [], "error": None, "collected_at": None, "stale": False, "collection_count": 0, "collection_interval_millis": 15000, "last_success_at": None, "truncated": False, "truncation_scope": []})
        snapshot.setdefault("logs", {"status": "unknown", "items": [], "retained_count": 0, "capacity": 100, "returned_count": 0, "evicted_count": 0})
        snapshot["links"] = build_links(settings, snapshot.get("instance_id"))
        for span in snapshot["spans"]:
            span["trace_url"] = trace_link(settings, span.get("trace_id"), snapshot.get("instance_id"))
        for item in snapshot.get("logs", {}).get("items", []):
            item["trace_url"] = trace_link(settings, item.get("trace_id"), snapshot.get("instance_id"))
        snapshot.setdefault("semconv_version", semconv_version)
        return snapshot

    return observation_snapshot


_OBSERVABILITY_START_LOCK = threading.RLock()


def start_observability(settings: Settings) -> Optional[Any]:
    if not settings.otel_enabled:
        return None
    with _OBSERVABILITY_START_LOCK:
        return _start_observability(settings)


def _start_observability(settings: Settings) -> Optional[Any]:
    """组合根统一的观测启动（HTTP lifespan 与 stdio main 复用）。

    顺序：setup_observability -> global instrumentors -> logging handlers
    -> Profile 采样器。禁用时返回 None 且零观测 import（不装埋点、不挂
    handler、不启动采样器）；启用失败（冲突/降级句柄）不阻断业务启动。
    """
    from app.application import telemetry_semantics, telemetry_details, telemetry_contract
    from app.infrastructure.observability.sanitizer import bind_details
    from app.infrastructure.observability.instrumentors import (
        install_global_instrumentors,
    )
    from app.infrastructure.observability.logging import (
        attach_observability_log_handlers,
        configure_execution_context,
        prepare_sanitizer,
    )
    from app.infrastructure.observability.setup import setup_observability, get_runtime_handle
    from app.infrastructure.observability.snapshots import safe_dimensions

    def metric_attributes(attributes, dimensions):
        projected = safe_dimensions(attributes, dimensions)
        model_key = telemetry_semantics.GEN_AI_REQUEST_MODEL
        configured_model = getattr(settings, "embedding_model", None)
        if (model_key in dimensions and attributes.get(model_key) == configured_model
                and telemetry_contract.valid_attribute(model_key, configured_model)):
            projected[model_key] = configured_model
        return projected

    def execution_attributes(attributes):
        projected = telemetry_contract.project_execution_attributes(attributes)
        model_key = telemetry_semantics.GEN_AI_REQUEST_MODEL
        if projected.get(model_key) != getattr(settings, "embedding_model", None):
            projected.pop(model_key, None)
        return projected

    def span_attributes(attributes):
        from app.infrastructure.observability.instrumentors import _filter_attributes
        projected = _filter_attributes({key:value for key,value in attributes.items()
                                       if not key.startswith("nkb.") and key not in ("db.statement", "db.query.text")})
        projected.update(execution_attributes(attributes))
        return projected

    def span_outcome(attributes, name):
        safe = execution_attributes(attributes)
        operation = telemetry_contract.OPERATIONS.get(name)
        obj = operation.object if operation else ("model" if name.startswith("embeddings") else "http")
        return safe.get(f"nkb.{obj}.outcome")

    active = get_runtime_handle()
    handle = setup_observability(
        settings,
        semantics=telemetry_semantics,
        semantics_keys={
            "outcome_resolver": span_outcome,
            "error_type": telemetry_semantics.ERROR_TYPE_ATTR,
            "attribute_projector": span_attributes,
            "metric_attribute_projector": metric_attributes,
        },
    )
    if handle is None or active is not None or not handle.is_published:
        return handle
    # 成本计价闭包：观测启用才装配（计价结果只服务于观测出口）
    _configure_embedding_cost_calculator(settings, handle)
    bind_details(
        configure=telemetry_details.configure_details,
        project=telemetry_details.project_details,
        validate=telemetry_details.validate_details,
        flatten=telemetry_details.flatten_details,
    )
    from app.application import telemetry_requests
    from app.infrastructure.observability.instrumentors import configure_request_context
    configure_request_context(scope_factory=telemetry_requests.request_scope,
                              snapshot=telemetry_requests.current_request_facts)
    handle.register_stop_callback(configure_request_context)
    install_global_instrumentors(settings=settings)
    # Capture one startup catalog for HTTP and stdio, never on a second app.
    import os
    import re
    sanitizer = prepare_sanitizer()
    sanitizer.configure_secret_catalog(tuple(value for key,value in os.environ.items()
        if re.search(r"api.?key|token|password|passwd|secret|credential", key, re.I) and value))
    configure_execution_context(snapshot=telemetry_semantics.current_operation_context,
        project=telemetry_details.project_details, validate=telemetry_details.validate_details,
        flatten=telemetry_details.flatten_details, details_snapshot=telemetry_details.current_details,
        budget=telemetry_details.budget_details, max_bytes=settings.otel_content_max_bytes,
        execution_project=execution_attributes)
    from app.infrastructure.observability.logging import emit_completion_snapshot, completion_scope
    telemetry_semantics.configure_completion(emit_completion_snapshot, scope_factory=completion_scope)
    handle.register_stop_callback(lambda: telemetry_semantics.configure_completion())
    register_application_log_events()
    attach_observability_log_handlers(bridge=handle.log_bridge)
    _start_profiler(handle, settings)
    return handle
