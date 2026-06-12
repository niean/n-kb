from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
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

    assert "../locals:/app/locals" in services["n-kb"]["volumes"]
    assert "../data:/app/data" in services["n-kb"]["volumes"]
    assert "/Users/niean/install/qdrant/storage:/qdrant/storage" in services["qdrant"]["volumes"]
    assert "/Users/niean/install/ollama:/root/.ollama" in services["ollama"]["volumes"]
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
