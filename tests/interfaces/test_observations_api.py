"""T9 只读自检 API（GET /observations/otel）接口测试。

覆盖：disabled/healthy/degraded、local/export 模式投递语义、部分失败恢复、
unknown 投递、分页边界（422）、空数据、并发快照安全与序列化哨兵（无凭据/
完整配置泄漏）。路由只消费注入的 snapshot callable，测试同样只经由
``build_observation_snapshot``（组合根）与 HTTP 合同验证。
"""
import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.composition import build_observation_snapshot
from app.config import Settings
from app.interfaces.http.routes import register_routes


def _signal(**overrides):
    base = {
        "status": "healthy",
        "delivery": "succeeded",
        "attempt_count": 3,
        "success_count": 3,
        "failure_count": 0,
        "last_success": "2026-10-30T00:00:00+00:00",
        "last_failure": None,
        "current_error": None,
    }
    base.update(overrides)
    return base


def _span(i: int):
    return {
        "name": f"span-{i}",
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": None,
        "status": "UNSET",
        "outcome": "ok",
        "error_type": None,
        "duration_ms": 1.5,
    }


def _healthy_snapshot(spans=None):
    return {
        "status": "healthy",
        "mode": "export",
        "instance_id": "instance-1",
        "sample_ratio": 0.5,
        "spans": list(spans if spans is not None else [_span(i) for i in range(12)]),
        "signals": {
            "traces": _signal(),
            "metrics": _signal(),
            "logs": _signal(),
            "profile": _signal(status="disabled", delivery="disabled"),
        },
        "dropped_after_deadline": 0,
        "diagnostic_error": None,
        "differing_fields": [],
        "semconv_version": "semconv-test-version",
    }


class FakeHandle:
    """最小 handle 替身：只实现 snapshot()（每次返回深副本）。"""

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.calls = 0
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            self.calls += 1
        return json.loads(json.dumps(self._snapshot))


def build_client(handle, settings=None):
    app = FastAPI()
    app.state.observation_snapshot = build_observation_snapshot(
        settings or Settings(_env_file=None), handle
    )
    register_routes(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# disabled 合同（handle=None 时注入完整合同的纯函数快照）
# ---------------------------------------------------------------------------


def test_disabled_returns_full_contract_when_handle_is_none():
    client = build_client(None)

    response = client.get("/observations/otel")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "disabled"
    assert body["mode"] == "disabled"
    assert body["instance_id"] is None
    assert body["sample_ratio"] == 1.0
    assert body["diagnostic_error"] is None
    assert body["spans"] == []
    assert body["pagination"] == {"page": 1, "size": 10, "total": 0, "pages": 0}
    assert set(body["signals"]) == {"traces", "metrics", "logs", "profile"}
    for signal in body["signals"].values():
        assert signal["status"] == "disabled"
        assert signal["delivery"] == "disabled"
        assert signal["attempt_count"] == 0
        assert signal["success_count"] == 0
        assert signal["failure_count"] == 0
        assert signal["last_success"] is None
        assert signal["last_failure"] is None
        assert signal["current_error"] is None


def test_disabled_contract_still_reports_semconv_version():
    from app.application import telemetry_semantics

    client = build_client(None)

    body = client.get("/observations/otel").json()

    assert body["semconv_version"] == telemetry_semantics.SEMCONV_VERSION


# ---------------------------------------------------------------------------
# healthy 透传与分页
# ---------------------------------------------------------------------------


def test_healthy_passthrough_and_first_page_pagination():
    client = build_client(FakeHandle(_healthy_snapshot()))

    body = client.get("/observations/otel").json()

    assert body["status"] == "healthy"
    assert body["mode"] == "export"
    assert body["instance_id"] == "instance-1"
    assert body["sample_ratio"] == 0.5
    assert body["semconv_version"] == "semconv-test-version"
    assert len(body["spans"]) == 10
    assert body["spans"][0]["name"] == "span-0"
    assert body["pagination"] == {"page": 1, "size": 10, "total": 12, "pages": 2}


def test_second_page_returns_remaining_spans():
    client = build_client(FakeHandle(_healthy_snapshot()))

    body = client.get("/observations/otel", params={"page": 2}).json()

    assert len(body["spans"]) == 2
    assert body["spans"][0]["name"] == "span-10"
    assert body["pagination"]["page"] == 2


def test_page_within_limit_but_beyond_data_returns_empty_spans():
    client = build_client(FakeHandle(_healthy_snapshot()))

    response = client.get("/observations/otel", params={"page": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["spans"] == []
    assert body["pagination"] == {"page": 10, "size": 10, "total": 12, "pages": 2}


def test_empty_data_returns_empty_page():
    client = build_client(FakeHandle(_healthy_snapshot(spans=[])))

    body = client.get("/observations/otel").json()

    assert body["spans"] == []
    assert body["pagination"] == {"page": 1, "size": 10, "total": 0, "pages": 0}


# ---------------------------------------------------------------------------
# degraded / 部分失败恢复 / unknown / local-only 投递语义
# ---------------------------------------------------------------------------


def test_degraded_diagnostic_handle_reports_degraded_not_disabled():
    snapshot = _healthy_snapshot()
    snapshot["status"] = "degraded"
    snapshot["diagnostic_error"] = "config_conflict"
    snapshot["differing_fields"] = ["otel_traces_sample_ratio"]
    client = build_client(FakeHandle(snapshot))

    body = client.get("/observations/otel").json()

    assert body["status"] == "degraded"
    assert body["diagnostic_error"] == "config_conflict"
    assert body["differing_fields"] == ["otel_traces_sample_ratio"]


def test_partial_failure_then_recovery_passthrough():
    failing = _healthy_snapshot()
    failing["status"] = "degraded"
    failing["signals"]["traces"] = _signal(
        delivery="failed",
        failure_count=2,
        last_failure="2026-10-30T00:01:00+00:00",
        current_error="export_failed",
    )
    client = build_client(FakeHandle(failing))

    body = client.get("/observations/otel").json()

    assert body["status"] == "degraded"
    traces = body["signals"]["traces"]
    assert traces["delivery"] == "failed"
    assert traces["failure_count"] == 2
    assert traces["last_failure"] == "2026-10-30T00:01:00+00:00"
    assert traces["current_error"] == "export_failed"

    recovered = _healthy_snapshot()
    recovered["signals"]["traces"] = _signal(failure_count=2)
    client = build_client(FakeHandle(recovered))

    body = client.get("/observations/otel").json()

    assert body["status"] == "healthy"
    traces = body["signals"]["traces"]
    assert traces["delivery"] == "succeeded"
    assert traces["failure_count"] == 2


def test_unknown_delivery_before_first_export_passthrough():
    snapshot = _healthy_snapshot()
    snapshot["signals"]["traces"] = _signal(
        delivery="unknown", attempt_count=0, success_count=0, last_success=None
    )
    client = build_client(FakeHandle(snapshot))

    body = client.get("/observations/otel").json()

    assert body["signals"]["traces"]["delivery"] == "unknown"


def test_local_mode_reports_disabled_delivery_not_unknown():
    snapshot = _healthy_snapshot(spans=[])
    snapshot["mode"] = "local"
    snapshot["signals"] = {
        "traces": _signal(status="healthy", delivery="disabled"),
        "metrics": _signal(status="healthy", delivery="disabled"),
        "logs": _signal(status="disabled", delivery="disabled"),
        "profile": _signal(status="healthy", delivery="unknown"),
    }
    client = build_client(FakeHandle(snapshot))

    body = client.get("/observations/otel").json()

    assert body["mode"] == "local"
    assert body["signals"]["traces"]["delivery"] == "disabled"
    assert body["signals"]["metrics"]["delivery"] == "disabled"
    assert body["signals"]["logs"]["delivery"] == "disabled"
    assert body["signals"]["profile"]["delivery"] == "unknown"


def test_profile_only_export_mode_passthrough():
    # Profile 自带网络上传出口（OTLP 无 endpoint）：mode=export（spec：
    # 任一 OTLP 或 Profile 网络出口启用为 export）；API 纯透传该语义。
    snapshot = _healthy_snapshot(spans=[])
    snapshot["mode"] = "export"
    snapshot["signals"] = {
        "traces": _signal(status="healthy", delivery="disabled"),
        "metrics": _signal(status="healthy", delivery="disabled"),
        "logs": _signal(status="disabled", delivery="disabled"),
        "profile": _signal(status="healthy", delivery="unknown"),
    }
    client = build_client(FakeHandle(snapshot))

    body = client.get("/observations/otel").json()

    assert body["mode"] == "export"
    assert body["signals"]["traces"]["delivery"] == "disabled"
    assert body["signals"]["profile"]["delivery"] == "unknown"
    assert body["status"] == "healthy"


# ---------------------------------------------------------------------------
# 分页参数校验（非法/负数/零/超界 422；size 只接受 10）
# ---------------------------------------------------------------------------


def test_page_zero_negative_and_out_of_range_return_422():
    client = build_client(FakeHandle(_healthy_snapshot()))

    for params in ({"page": 0}, {"page": -1}, {"page": 11}, {"page": 100}):
        response = client.get("/observations/otel", params=params)
        assert response.status_code == 422, params


def test_non_integer_page_returns_422():
    client = build_client(FakeHandle(_healthy_snapshot()))

    response = client.get("/observations/otel", params={"page": "abc"})

    assert response.status_code == 422


def test_size_other_than_10_returns_422():
    client = build_client(FakeHandle(_healthy_snapshot()))

    for params in ({"size": 9}, {"size": 11}, {"size": 0}, {"size": -10}, {"size": 1}):
        response = client.get("/observations/otel", params=params)
        assert response.status_code == 422, params


def test_size_10_is_accepted():
    client = build_client(FakeHandle(_healthy_snapshot()))

    response = client.get("/observations/otel", params={"size": 10})

    assert response.status_code == 200


def test_page_1_and_page_10_boundaries_accepted():
    client = build_client(FakeHandle(_healthy_snapshot()))

    assert client.get("/observations/otel", params={"page": 1}).status_code == 200
    assert client.get("/observations/otel", params={"page": 10}).status_code == 200


# ---------------------------------------------------------------------------
# Grafana 外链（仅配置时输出，仅供外链）
# ---------------------------------------------------------------------------


def test_grafana_urls_present_only_when_configured():
    settings = Settings(
        _env_file=None,
        otel_grafana_base_url="https://grafana.example.com",
        otel_grafana_traces_base_url="https://grafana.example.com/explore",
    )
    client = build_client(FakeHandle(_healthy_snapshot()), settings=settings)

    body = client.get("/observations/otel").json()

    assert body["grafana"] == {
        "base_url": "https://grafana.example.com",
        "traces_base_url": "https://grafana.example.com/explore",
    }


def test_grafana_urls_null_when_not_configured():
    client = build_client(FakeHandle(_healthy_snapshot()))

    body = client.get("/observations/otel").json()

    assert body["grafana"] == {"base_url": None, "traces_base_url": None}


# ---------------------------------------------------------------------------
# 序列化哨兵：无凭据、无完整配置泄漏
# ---------------------------------------------------------------------------


def test_response_never_contains_credentials_or_full_config():
    settings = Settings(
        _env_file=None,
        otel_exporter_otlp_endpoint="http://collector-sentinel-abc:4317",
        profiling_pyroscope_endpoint="http://pyro-sentinel-abc:4040",
    )
    client = build_client(FakeHandle(_healthy_snapshot()), settings=settings)

    response = client.get("/observations/otel")
    text = response.text
    body_keys = set(response.json().keys())

    assert "collector-sentinel-abc" not in text
    assert "pyro-sentinel-abc" not in text
    assert "otel_exporter_otlp_endpoint" not in text
    assert "profiling_pyroscope_endpoint" not in text
    assert "settings" not in body_keys
    assert "config" not in body_keys


# ---------------------------------------------------------------------------
# 并发快照安全
# ---------------------------------------------------------------------------


def test_concurrent_snapshot_calls_are_thread_safe():
    handle = FakeHandle(_healthy_snapshot())
    client = build_client(handle)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        for page in (1, 2):
            try:
                response = client.get("/observations/otel", params={"page": page})
                with lock:
                    results.append((page, response.status_code, response.json()))
            except Exception as exc:  # pragma: no cover - 失败时收集诊断
                with lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 16
    for page, status_code, body in results:
        assert status_code == 200
        assert body["pagination"]["page"] == page
        assert body["status"] == "healthy"
    assert handle.calls == 16


# ---------------------------------------------------------------------------
# create_app 注入（disabled 默认 + lifespan 后可用）
# ---------------------------------------------------------------------------


def test_create_app_wires_disabled_snapshot_callable():
    from app.main import create_app

    app = create_app(Settings(_env_file=None, mcp_enabled=False))

    # lifespan 之前：callable 已存在且报告 disabled（不误报错误）。
    snapshot = app.state.observation_snapshot(page=1, size=10)
    assert snapshot["status"] == "disabled"

    with TestClient(app) as client:
        response = client.get("/observations/otel")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "disabled"


# ---------------------------------------------------------------------------
# 防御性兜底与形状一致性（质量加固）
# ---------------------------------------------------------------------------


class _ExplodingHandle:
    """snapshot() 抛异常的句柄替身：T4 合同不抛出，此为防御分支。"""

    def snapshot(self):
        raise RuntimeError("boom")


def test_snapshot_failure_falls_back_to_degraded_payload(capsys):
    client = build_client(_ExplodingHandle())

    response = client.get("/observations/otel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["mode"] == "unknown"
    assert body["diagnostic_error"] == "snapshot_failed"
    assert body["spans"] == []
    assert body["pagination"] == {"page": 1, "size": 10, "total": 0, "pages": 0}
    # 根因写一行有界 stderr 诊断（不含原异常文本）。
    assert "snapshot() failed" in capsys.readouterr().err
    assert "boom" not in capsys.readouterr().err


def test_disabled_and_live_snapshot_shapes_pinned():
    """禁用合同与启用快照的顶层形状一致（允许集合漂移被显式钉住）。"""
    live_snapshot = _healthy_snapshot()
    live_snapshot["instruments"] = []  # T4 启用快照携带仪表列表
    live_client = build_client(FakeHandle(live_snapshot))
    disabled_client = build_client(None)

    live_keys = set(live_client.get("/observations/otel").json())
    disabled_keys = set(disabled_client.get("/observations/otel").json())

    # 允许的漂移：启用快照独有 instruments（T4 仪表列表）；除此必须一致。
    assert live_keys - disabled_keys == {"instruments"}
    assert disabled_keys - live_keys == set()


def test_disabled_metric_summary_contract_matches_runtime_summary():
    from app.infrastructure.observability.snapshots import MetricSnapshot
    body = build_client(None).get('/observations/otel').json()
    assert set(body['metric_snapshot']) == set(MetricSnapshot().latest())
    assert body['metric_snapshot']['status'] == 'disabled'
