"""约束 pyproject.toml 中的依赖版本，避免上游破坏性升级导致部署失败。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"


def _load_dependencies() -> list[str]:
    with PYPROJECT_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["dependencies"])


def _select_dependency(dependencies: list[str], name: str) -> str:
    matches = [item for item in dependencies if item.split(";", 1)[0].strip().split("==")[0].split(">=")[0].split(">")[0].split("<=")[0].split("<")[0].split("~=")[0].split("!=")[0].strip().lower() == name.lower()]
    assert matches, f"dependency {name!r} is missing from pyproject.toml"
    return matches[0]


def test_pyproject_pins_mcp_below_v2() -> None:
    """mcp 2.x 移除了 `mcp.server.fastmcp`（重命名为 MCPServer），代码仍按 v1 API 编写。

    部署依赖 `pip install .` 在不指定版本约束时会解析到 mcp 2.x，导致容器启动失败。
    """
    dependencies = _load_dependencies()
    mcp_spec = _select_dependency(dependencies, "mcp")

    assert "<2" in mcp_spec or "<2.0" in mcp_spec, (
        f"mcp dependency must be pinned below v2 to keep the v1 FastMCP API working; got {mcp_spec!r}"
    )
    assert ">=2" not in mcp_spec.replace(">=1.27,<2", ""), (
        f"mcp dependency must not allow v2 or above; got {mcp_spec!r}"
    )


OTEL_PINNED_DEPENDENCIES = {
    "opentelemetry-api": "1.45.1",
    "opentelemetry-sdk": "1.45.1",
    "opentelemetry-exporter-otlp-proto-grpc": "1.45.1",
    "opentelemetry-instrumentation-fastapi": "0.66b1",
    "opentelemetry-instrumentation-httpx": "0.66b1",
    "opentelemetry-instrumentation-sqlite3": "0.66b1",
}


def test_pyproject_pins_otel_observability_dependencies() -> None:
    """spec 六项 OTel 依赖必须精确 pin。

    版本来源 N-Agent 已验证安装集合（opentelemetry-api/sdk/exporter-otlp-proto-grpc
    ==1.45.1，instrumentation-fastapi/httpx/sqlite3 ==0.66b1），非本仓独立验证事实。
    """
    dependencies = _load_dependencies()

    for name, version in OTEL_PINNED_DEPENDENCIES.items():
        spec = _select_dependency(dependencies, name)
        expected = f"{name}=={version}"
        assert spec == expected, (
            f"{name} must be pinned exactly to {expected!r} for observability; got {spec!r}"
        )


def test_fastapi_testclient_imports_without_deprecation_warning() -> None:
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "from fastapi.testclient import TestClient"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_system_metrics_matches_nagent_instrumentation_version():
    assert _select_dependency(_load_dependencies(), 'opentelemetry-instrumentation-system-metrics') == 'opentelemetry-instrumentation-system-metrics==0.66b1'
