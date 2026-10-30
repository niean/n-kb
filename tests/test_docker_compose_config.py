from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE_EXAMPLE = PROJECT_ROOT / "docker" / "docker-compose.yml.example"
OBSERVABILITY_OVERRIDE = PROJECT_ROOT / "docker" / "observability" / "compose.yml"
ENV_EXAMPLE = PROJECT_ROOT / "docker" / ".env.example"
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"

# 观测九配置：默认与 app/config.py 一致；include 文件提供两个部署 endpoint 默认值。
OBSERVABILITY_ENV_DEFAULTS = {
    "N_KB_OTEL_ENABLED": "false",
    "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT": "",
    "N_KB_OTEL_TRACES_SAMPLE_RATIO": "1.0",
    "N_KB_OTEL_CONTENT_MAX_BYTES": "2048",
    "N_KB_OTEL_GRAFANA_BASE_URL": "",
    "N_KB_OTEL_GRAFANA_TRACES_BASE_URL": "",
    "N_KB_OTEL_GRAFANA_METRICS_BASE_URL": "",
    "N_KB_OTEL_GRAFANA_LOGS_BASE_URL": "",
    "N_KB_OTEL_GRAFANA_PROFILE_BASE_URL": "",
    "N_KB_PROFILING_ENABLED": "false",
    "N_KB_PROFILING_PYROSCOPE_ENDPOINT": "",
    "N_KB_PROFILING_INTERVAL_SECONDS": "60",
}
IGNORED_COMPOSE_DIRS = {
    ".git",
    ".harness",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data",
    "locals",
    "logs",
}


def iter_compose_files() -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.rglob("docker-compose*.yml")
        if not any(part in IGNORED_COMPOSE_DIRS for part in path.relative_to(PROJECT_ROOT).parts)
    )


@pytest.fixture(params=iter_compose_files(), ids=lambda path: str(path.relative_to(PROJECT_ROOT)))
def compose_file(request):
    return request.param


def test_docker_compose_files_are_discovered_recursively():
    compose_files = {path.relative_to(PROJECT_ROOT) for path in iter_compose_files()}

    assert Path("docker/docker-compose.yml") in compose_files
    assert Path("docker-compose.yml") not in compose_files
    assert not any(path.parts[0] in {"locals", "data", "logs"} for path in compose_files)


def test_docker_compose_declares_expected_services_ports_and_volumes(compose_file):
    compose_config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))

    services = compose_config["services"]

    assert {"n-kb", "qdrant", "ollama"}.issubset(services)
    assert "8212:8212" in services["n-kb"]["ports"]
    assert "6333:6333" in services["qdrant"]["ports"]
    assert "11434:11434" in services["ollama"]["ports"]

    for service in services.values():
        assert "network_mode" not in service

    n_kb_environment = services["n-kb"]["environment"]
    assert n_kb_environment["N_KB_MCP_ENABLED"] == "${N_KB_MCP_ENABLED:-false}"
    assert n_kb_environment["N_KB_MCP_PATH"] == "${N_KB_MCP_PATH:-/mcp}"
    assert n_kb_environment["N_KB_MCP_NAME"] == "${N_KB_MCP_NAME:-N-KB MCP}"
    assert n_kb_environment["N_KB_MCP_ALLOWED_HOSTS"] == (
        "${N_KB_MCP_ALLOWED_HOSTS:-127.0.0.1:*,localhost:*,[::1]:*,nkb.localhost,nkb.localhost:*,n-kb,n-kb:*}"
    )
    assert n_kb_environment["N_KB_MCP_ALLOWED_ORIGINS"] == (
        "${N_KB_MCP_ALLOWED_ORIGINS:-http://127.0.0.1:*,http://localhost:*,http://[::1]:*,http://nkb.localhost,http://nkb.localhost:*,http://n-kb,http://n-kb:*}"
    )

    assert "../locals:/app/locals" in services["n-kb"]["volumes"]
    assert "../data:/app/data" in services["n-kb"]["volumes"]
    assert any(
        volume.endswith(":/qdrant/storage") for volume in services["qdrant"]["volumes"]
    )
    assert any(
        volume.endswith(":/root/.ollama") for volume in services["ollama"]["volumes"]
    )
    assert "volumes" not in compose_config


def test_docker_compose_pins_known_images(compose_file):
    compose_config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))

    qdrant_image = compose_config["services"]["qdrant"]["image"]
    ollama_image = compose_config["services"]["ollama"]["image"]

    assert qdrant_image.startswith("qdrant/qdrant:v1.18.2@sha256:")
    assert ollama_image.startswith("ollama/ollama@sha256:")


def test_docker_compose_bootstraps_ollama_bge_m3_model(compose_file):
    compose_config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = compose_config["services"]

    bootstrap_service = services["ollama-pull-bge-m3"]

    assert bootstrap_service["image"] == services["ollama"]["image"]
    assert bootstrap_service["environment"]["OLLAMA_HOST"] == "http://ollama:11434"
    assert bootstrap_service["entrypoint"] == ["/bin/sh"]
    assert "ollama" in bootstrap_service["depends_on"]
    command = bootstrap_service["command"]
    command_text = "\n".join(command) if isinstance(command, list) else command
    assert "http://ollama:11434/api/tags" in command_text
    assert "ollama pull bge-m3" in command_text

    n_kb_depends_on = services["n-kb"]["depends_on"]
    assert "ollama-pull-bge-m3" in n_kb_depends_on


def test_docker_compose_config_is_valid(compose_file):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is unavailable; skipping docker compose config validation")

    result = subprocess.run(
        ["docker", "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("docker compose plugin is unavailable; skipping docker compose config validation")

    result = subprocess.run(
        ["docker", "compose", "-f", compose_file.name, "config"],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# T11: 观测镜像、Compose override 与部署说明。
# ---------------------------------------------------------------------------


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result.returncode == 0


def _merged_compose_config(flags: list[str], env_file_text: str = "") -> dict:
    # 使用独立 env 文件隔离宿主机与本机 docker/.env 的干扰，保证断言确定性。
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
        handle.write(env_file_text)
        env_file_path = handle.name
    try:
        result = subprocess.run(
            ["docker", "compose", f"--env-file={env_file_path}", *flags, "config"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    finally:
        os.unlink(env_file_path)
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


def test_base_example_maps_all_observability_environment_defaults():
    compose_config = yaml.safe_load(BASE_COMPOSE_EXAMPLE.read_text(encoding="utf-8"))
    environment = compose_config["services"]["n-kb"]["environment"]

    included_environment = {
        "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT",
        "N_KB_PROFILING_PYROSCOPE_ENDPOINT",
    }
    for name, default in OBSERVABILITY_ENV_DEFAULTS.items():
        if name in included_environment:
            assert name not in environment
        else:
            assert environment[name] == f"${{{name}:-{default}}}"


def test_base_example_stop_grace_period_is_at_least_10s():
    compose_config = yaml.safe_load(BASE_COMPOSE_EXAMPLE.read_text(encoding="utf-8"))
    grace_period = compose_config["services"]["n-kb"]["stop_grace_period"]

    match = re.fullmatch(r"(\d+)s", str(grace_period))
    assert match is not None, f"unexpected stop_grace_period: {grace_period}"
    assert int(match.group(1)) >= 10


def test_env_example_declares_observability_defaults():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")

    for name, default in OBSERVABILITY_ENV_DEFAULTS.items():
        # 允许注释形态（默认值），取消注释即覆盖。
        assert f"{name}={default}" in env_text, name


def test_dockerfile_pins_verified_py_spy_and_checks_version():
    dockerfile_text = DOCKERFILE.read_text(encoding="utf-8")

    # 0.4.2：N-Agent T9 S1 核验过与 grafana/pyroscope:1.21.1 /ingest 契约兼容。
    assert "py-spy==0.4.2" in dockerfile_text
    assert "py-spy --version" in dockerfile_text


def test_compose_plan_commands_parse_cleanly():
    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate compose config")

    flag_sets = [
        ["-f", "docker/docker-compose.yml.example"],
        ["-f", "docker/docker-compose.yml"],
    ]
    for flags in flag_sets:
        result = subprocess.run(
            ["docker", "compose", *flags, "config", "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{flags}: {result.stderr}"


def test_base_merged_config_resolves_observability_defaults():
    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate merged config")

    config = _merged_compose_config(["-f", str(BASE_COMPOSE_EXAMPLE)])
    n_kb = config["services"]["n-kb"]

    included_defaults = {
        **OBSERVABILITY_ENV_DEFAULTS,
        "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4317",
        "N_KB_PROFILING_PYROSCOPE_ENDPOINT": "http://pyroscope:4040",
    }
    for name, default in included_defaults.items():
        assert n_kb["environment"][name] == default, name
    assert str(n_kb["stop_grace_period"]) == "15s"
    assert set(n_kb.get("depends_on", {})) <= {"qdrant", "ollama", "ollama-pull-bge-m3"}
    assert n_kb.get("cap_add") == ["SYS_PTRACE"]
    assert not n_kb.get("privileged", False)


def test_restart_compose_entry_includes_observability_config():
    active_compose = PROJECT_ROOT / "docker" / "docker-compose.yml"
    for compose_file in (active_compose, BASE_COMPOSE_EXAMPLE):
        compose_config = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        assert compose_config["include"] == ["observability/compose.yml"]
    assert OBSERVABILITY_OVERRIDE.is_file()

    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate merged config")
    config = _merged_compose_config(["-f", str(active_compose)])
    n_kb = config["services"]["n-kb"]
    assert set(n_kb["networks"]) == {"default", "n-agent-observability"}
    assert n_kb["environment"]["N_KB_OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://otel-collector:4317"
    )


def test_observability_override_merges_external_network_and_endpoint():
    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate merged config")

    config = _merged_compose_config(
        ["-f", str(BASE_COMPOSE_EXAMPLE)]
    )
    n_kb = config["services"]["n-kb"]

    # 网络：n-kb 同时挂在 default 与 external 观测网络，后端服务不追加。
    # 默认接入网络为 n-telemetry（n-observability 栈的 otel-collector/pyroscope 所在）。
    assert set(n_kb["networks"]) == {"default", "n-agent-observability"}
    network = config["networks"]["n-agent-observability"]
    assert network["external"] is True
    assert network["name"] == "n-telemetry"
    for service_name in ("qdrant", "ollama"):
        assert "n-agent-observability" not in config["services"][service_name].get(
            "networks", []
        )

    # collector endpoint 默认指向 N-Agent otel-collector；统一文件保留最小采样权限。
    assert n_kb["environment"]["N_KB_OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://otel-collector:4317"
    )
    assert n_kb.get("cap_add") == ["SYS_PTRACE"]
    assert not n_kb.get("privileged", False)
    assert str(n_kb["stop_grace_period"]) == "15s"
    assert set(n_kb.get("depends_on", {})) <= {"qdrant", "ollama", "ollama-pull-bge-m3"}


def test_observability_override_merges_sys_ptrace_and_pyroscope_defaults():
    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate merged config")

    config = _merged_compose_config(
        ["-f", str(BASE_COMPOSE_EXAMPLE)]
    )
    n_kb = config["services"]["n-kb"]

    assert "SYS_PTRACE" in n_kb.get("cap_add", [])
    assert not n_kb.get("privileged", False)
    assert n_kb["environment"]["N_KB_PROFILING_ENABLED"] == "false"
    assert n_kb["environment"]["N_KB_PROFILING_PYROSCOPE_ENDPOINT"] == "http://pyroscope:4040"
    assert str(n_kb["stop_grace_period"]) == "15s"


def test_observability_override_respects_user_env_values():
    if not _docker_compose_available():
        pytest.skip("docker compose is unavailable; cannot validate merged config")

    env_text = (
        "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.internal:4317\n"
        "N_KB_PROFILING_PYROSCOPE_ENDPOINT=http://pyroscope.internal:4040\n"
        "N_KB_OBS_EXTERNAL_NETWORK=n-telemetry\n"
    )
    config = _merged_compose_config(
        ["-f", str(BASE_COMPOSE_EXAMPLE)],
        env_file_text=env_text,
    )
    n_kb = config["services"]["n-kb"]

    # 用户值优先生效，override 默认值不硬编码覆盖。
    assert n_kb["environment"]["N_KB_OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://collector.internal:4317"
    )
    assert n_kb["environment"]["N_KB_PROFILING_PYROSCOPE_ENDPOINT"] == (
        "http://pyroscope.internal:4040"
    )
    assert config["networks"]["n-agent-observability"]["name"] == "n-telemetry"


def test_docker_image_build_installs_pinned_py_spy():
    if os.environ.get("N_KB_DOCKER_BUILD_TEST") != "1":
        pytest.skip(
            "set N_KB_DOCKER_BUILD_TEST=1 to run the real image build "
            "(slow and needs network); default runs assert the pinned Dockerfile instead"
        )
    if not _docker_compose_available():
        pytest.skip("docker is unavailable; cannot build image")

    image_tag = "n-kb-py-spy-build-check"
    try:
        build = subprocess.run(
            ["docker", "build", "-f", "docker/Dockerfile", "-t", image_tag, "."],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "docker build 超过 1800s 未返回；buildx 后台进程可能持有输出管道，"
            "请检查 docker 状态后重跑"
        )
    assert build.returncode == 0, build.stderr

    try:
        run = subprocess.run(
            ["docker", "run", "--rm", image_tag, "py-spy", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert run.returncode == 0, run.stderr
        assert "0.4.2" in run.stdout + run.stderr
    finally:
        subprocess.run(
            ["docker", "rmi", "-f", image_tag],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
