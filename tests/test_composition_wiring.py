"""T9 组合根接线测试：仅断言装配与 lifespan 启停，不发真实网络。

组合根（build_services + main.lifespan）负责：
- 构造 CorpusRevisionService / EvaluationCapture 并注入 RetrievalService；
- 注入 evaluation_control_register 到 DocumentService（T8 裁决护栏）；
- endpoint 与 token 齐备时构造 ProducerClient + EvaluationDeliveryWorker 并
  暴露 delivery_worker/delivery_client；缺失时不构造网络对象（本地捕获与
  清理照常）；
- lifespan 启动期调用一次 retry_pending_cleanups 并周期调度；启动 worker，
  关闭时先 close(15) 再回收 client aclose()。

测试通过替换 app.composition.ProducerClient 为桩实现隔离网络。
"""
import types

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
import app.composition as composition
from app.application.corpus_revision import (
    CorpusRevisionConfig,
    CorpusRevisionService,
    fingerprint_from_splitter,
)
from app.application.evaluation_capture import EvaluationCapture
from app.application.evaluation_delivery import EvaluationDeliveryWorker
from app.config import Settings

EVALUATION_ENV_VARS = (
    "N_KB_EVALUATION_CAPTURE_ENABLED",
    "N_KB_EVALUATION_ENDPOINT_URL",
    "N_KB_EVALUATION_SERVICE_TOKEN",
)


class StubProducerClient:
    """组合根接线桩：记录构造参数、send 调用与 aclose 生命周期。"""

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, message_bytes: bytes) -> types.SimpleNamespace:
        self.sent.append(message_bytes)
        return types.SimpleNamespace(
            kind="accepted", code=None, retry_after=None, body={"status": "accepted"}
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_evaluation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in EVALUATION_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        _env_file=None,
        sqlite_path=tmp_path / "n-kb.db",
        storage_root=tmp_path / "data",
        **extra,
    )


# --- 装配断言（不启网络） ---------------------------------------------------


def test_build_services_wires_embedding_timeout_from_settings(tmp_path):
    settings = _settings(tmp_path)

    services = composition.build_services(settings)

    provider = services.retrieval._embedding_provider
    assert provider.client.timeout.read == settings.embedding_timeout_seconds


def test_build_services_wires_embedding_keep_alive_and_exposes_provider(tmp_path):
    settings = _settings(tmp_path)

    services = composition.build_services(settings)

    provider = services.retrieval._embedding_provider
    assert provider is services.embedding_provider
    assert provider._keep_alive == settings.embedding_keep_alive


def test_build_services_wires_evaluation_components_without_credentials(tmp_path):
    settings = _settings(tmp_path)

    services = composition.build_services(settings)

    # 凭据缺失：不构造网络投递对象（本地捕获/清理照常）
    assert services.delivery_worker is None
    assert services.delivery_client is None
    # capture_enabled 默认 false：hook 始终注入（context 组装/计数开关语义不变）
    assert isinstance(services.retrieval._capture_hook, EvaluationCapture)
    assert services.retrieval._capture_hook._enabled is False
    # corpus revision 服务注入 RetrievalService，指纹来自实际 splitter
    revision = services.retrieval._corpus_revision
    assert isinstance(revision, CorpusRevisionService)
    assert isinstance(revision._config, CorpusRevisionConfig)
    assert revision._config.embedding_model == settings.embedding_model


def test_build_services_corpus_revision_fingerprint_from_splitter(tmp_path):
    from app.infrastructure.parsers.text_parser import SimpleTextSplitter

    settings = _settings(tmp_path)
    services = composition.build_services(settings)

    revision = services.retrieval._corpus_revision
    expected = fingerprint_from_splitter(SimpleTextSplitter())
    assert revision._config.chunking_fingerprint == expected


def test_build_services_injects_evaluation_control_register(tmp_path):
    # T8 审阅裁决：生产组合根必须注入原子删除端口，禁止静默回退非原子删除
    settings = _settings(tmp_path)

    services = composition.build_services(settings)

    assert services.documents._evaluation_control_register is not None


def test_build_services_constructs_delivery_worker_when_credentials_present(
    tmp_path, monkeypatch
):
    stub_types = []

    def stub_factory(endpoint, token):
        stub = StubProducerClient(endpoint, token)
        stub_types.append(stub)
        return stub

    monkeypatch.setattr(composition, "ProducerClient", stub_factory)
    settings = _settings(
        tmp_path,
        evaluation_capture_enabled=True,
        evaluation_endpoint_url="http://127.0.0.1:1",
        evaluation_service_token="token",
    )

    services = composition.build_services(settings)

    assert isinstance(services.delivery_worker, EvaluationDeliveryWorker)
    assert services.delivery_client is stub_types[0]
    assert services.delivery_client.endpoint == "http://127.0.0.1:1"
    assert services.retrieval._capture_hook._enabled is True


# --- lifespan 启停（stub 网络客户端） ---------------------------------------


def test_embedding_warmup_calls_provider_once_and_swallows_failure():
    class StubProvider:
        def __init__(self, fail=False):
            self.calls = 0
            self.queries = []
            self.fail = fail

        def embed_query(self, query):
            self.calls += 1
            self.queries.append(query)
            if self.fail:
                raise RuntimeError("embedding_provider_failed")

    ok = StubProvider()
    app_main._warmup_embedding(ok)
    assert ok.calls == 1

    failing = StubProvider(fail=True)
    app_main._warmup_embedding(failing)
    assert failing.calls == 1


def _patch_composition_client(monkeypatch):
    stubs = []

    def stub_factory(endpoint, token):
        stub = StubProducerClient(endpoint, token)
        stubs.append(stub)
        return stub

    monkeypatch.setattr(composition, "ProducerClient", stub_factory)
    return stubs


def _capture_services(monkeypatch):
    captured = {}
    real_build = app_main.build_services

    def spy(settings, **kwargs):
        services = real_build(settings, **kwargs)
        captured["services"] = services
        return services

    monkeypatch.setattr(app_main, "build_services", spy)
    return captured


def test_lifespan_starts_and_stops_delivery_worker_and_client(tmp_path, monkeypatch):
    stubs = _patch_composition_client(monkeypatch)
    captured = _capture_services(monkeypatch)
    settings = _settings(
        tmp_path,
        evaluation_endpoint_url="http://127.0.0.1:1",
        evaluation_service_token="token",
    )
    app = app_main.create_app(settings)

    with TestClient(app):
        services = captured["services"]
        worker = services.delivery_worker
        assert worker is not None
        # worker 循环已启动（run 记录了当前任务）
        assert worker._run_task is not None
        _wait_for_condition(lambda: bool(stubs[0].sent))

    # 关闭：worker 循环终止，组合根回收投递客户端
    assert stubs[0].closed is True
    assert worker._run_task.done()


def test_lifespan_without_credentials_does_not_start_network_loop(
    tmp_path, monkeypatch
):
    stubs = _patch_composition_client(monkeypatch)
    captured = _capture_services(monkeypatch)
    settings = _settings(tmp_path)
    app = app_main.create_app(settings)

    with TestClient(app):
        assert captured["services"].delivery_worker is None
        assert stubs == []

    assert stubs == []


def test_lifespan_runs_startup_recovery_once(tmp_path, monkeypatch):
    calls = []
    settings = _settings(tmp_path)
    app = app_main.create_app(settings)

    # 单 patch：在 build_services 返回后、lifespan 恢复调用前替换恢复入口
    real_build = app_main.build_services

    def recording_build(settings, **kwargs):
        services = real_build(settings, **kwargs)
        monkeypatch.setattr(
            services.documents,
            "retry_pending_cleanups",
            lambda: calls.append("recovery") and 0,
        )
        return services

    monkeypatch.setattr(app_main, "build_services", recording_build)

    with TestClient(app):
        pass

    # 启动期恢复执行一次（lifespan 内调用，先于 yield）；周期兜底调度间隔
    # 60s，测试窗口内不会再次触发
    assert calls == ["recovery"]


def test_lifespan_startup_recovery_store_busy_does_not_abort(tmp_path, monkeypatch):
    """启动期恢复遇读侧 busy 突发：记日志后继续启动（交给周期兜底）。"""
    settings = _settings(tmp_path)
    app = app_main.create_app(settings)

    real_build = app_main.build_services

    def busy_build(settings, **kwargs):
        import sqlite3

        services = real_build(settings, **kwargs)

        def busy():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(services.documents, "retry_pending_cleanups", busy)
        return services

    monkeypatch.setattr(app_main, "build_services", busy_build)

    with TestClient(app) as client:
        # 应用正常启动并服务请求，启动未被 busy 突发中止
        assert client.get("/health").status_code == 200


class DyingWorker:
    """run() 立即死亡（非 busy 存储故障快速失败路径）的投递 worker 桩。"""

    def __init__(self) -> None:
        self.closed = False

    async def run(self) -> None:
        raise RuntimeError("delivery loop boom")

    async def close(self, wait_seconds: float = 15) -> None:
        self.closed = True


def test_lifespan_logs_dead_delivery_worker_and_shuts_down(tmp_path, monkeypatch, caplog):
    import dataclasses
    import logging

    captured = _capture_services(monkeypatch)
    settings = _settings(
        tmp_path,
        evaluation_endpoint_url="http://127.0.0.1:1",
        evaluation_service_token="token",
    )
    app = app_main.create_app(settings)

    # 用 run() 必抛的 worker 桩替换真实 worker（组合根其余装配不变）
    real_build = app_main.build_services

    def dying_build(settings, **kwargs):
        services = real_build(settings, **kwargs)
        captured["client"] = services.delivery_client
        replaced = dataclasses.replace(services, delivery_worker=DyingWorker())
        captured["services"] = replaced
        return replaced

    monkeypatch.setattr(app_main, "build_services", dying_build)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        with TestClient(app):
            # worker 任务死亡后错误日志可见（只记异常类型名）
            _wait_for_condition(
                lambda: any(
                    "evaluation delivery worker died" in record.message
                    for record in caplog.records
                )
            )

    death_records = [
        record for record in caplog.records
        if "evaluation delivery worker died" in record.message
    ]
    assert any("RuntimeError" in record.getMessage() for record in death_records)
    # 应用正常关闭（stub close 被调用，lifespan 未被死亡任务破坏）
    assert captured["services"].delivery_worker.closed is True
    assert captured["client"] is not None


@pytest.mark.asyncio
async def test_cleanup_retry_loop_survives_repeated_failures(monkeypatch):
    """周期兜底恢复契约：持续失败不杀死循环、间隔生效不热循环、cancel 退出。"""
    import asyncio
    from contextlib import suppress

    monkeypatch.setattr(app_main, "_EVALUATION_CLEANUP_RETRY_INTERVAL_SECONDS", 0.02)
    calls = []

    class FlakyDocuments:
        def retry_pending_cleanups(self):
            calls.append("call")
            raise RuntimeError("boom")

    task = asyncio.create_task(app_main._cleanup_retry_loop(FlakyDocuments()))
    await asyncio.sleep(0.12)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # 单次异常不杀死循环（0.12s 内多次触发）；间隔生效（热循环会是数千次）
    assert 1 < len(calls) <= 10


def _wait_for_condition(condition, timeout_seconds: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("condition not met within timeout")
