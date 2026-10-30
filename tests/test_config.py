from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


N_KB_ENV_VARS = (
    "N_KB_SQLITE_PATH",
    "N_KB_STORAGE_ROOT",
    "N_KB_QDRANT_URL",
    "N_KB_QDRANT_COLLECTION",
    "N_KB_EMBEDDING_BASE_URL",
    "N_KB_EMBEDDING_MODEL",
    "N_KB_INGESTION_BATCH_SIZE",
    "N_KB_MAX_UPLOAD_BYTES",
    "N_KB_ALLOWED_FILE_EXTENSIONS",
    "N_KB_MCP_ENABLED",
    "N_KB_MCP_PATH",
    "N_KB_MCP_NAME",
    "N_KB_MCP_ALLOWED_HOSTS",
    "N_KB_MCP_ALLOWED_ORIGINS",
)


def test_settings_defaults_are_local_paths(monkeypatch):
    for env_var in N_KB_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

    settings = Settings(_env_file=None)

    assert settings.sqlite_path == Path("locals/n-kb.db")
    assert settings.storage_root == Path("data")
    assert settings.qdrant_url == "http://localhost:6333"
    assert settings.qdrant_collection == "n_kb_documents"
    assert settings.embedding_base_url == "http://localhost:11434"
    assert settings.embedding_model == "bge-m3"
    assert settings.ingestion_batch_size == 16
    assert settings.max_upload_bytes == 2 * 1024 * 1024
    assert settings.allowed_file_extensions == {".md", ".txt"}
    assert settings.mcp_enabled is False
    assert settings.mcp_path == "/mcp"
    assert settings.mcp_name == "N-KB MCP"
    assert settings.mcp_allowed_hosts == [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "nkb.localhost",
        "nkb.localhost:*",
        "n-kb",
        "n-kb:*",
    ]
    assert settings.mcp_allowed_origins == [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "http://nkb.localhost",
        "http://nkb.localhost:*",
        "http://n-kb",
        "http://n-kb:*",
    ]


def test_settings_reads_n_kb_prefixed_environment(monkeypatch):
    monkeypatch.setenv("N_KB_SQLITE_PATH", "locals/test.db")
    monkeypatch.setenv("N_KB_STORAGE_ROOT", "tmp/storage")
    monkeypatch.setenv("N_KB_QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("N_KB_QDRANT_COLLECTION", "test_collection")
    monkeypatch.setenv("N_KB_EMBEDDING_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("N_KB_EMBEDDING_MODEL", "test-model")
    monkeypatch.setenv("N_KB_INGESTION_BATCH_SIZE", "32")
    monkeypatch.setenv("N_KB_MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setenv("N_KB_ALLOWED_FILE_EXTENSIONS", ".md,.txt")
    monkeypatch.setenv("N_KB_MCP_ENABLED", "true")
    monkeypatch.setenv("N_KB_MCP_PATH", "/kb-mcp")
    monkeypatch.setenv("N_KB_MCP_NAME", "Custom MCP")
    monkeypatch.setenv("N_KB_MCP_ALLOWED_HOSTS", "nkb.localhost,nkb.localhost:8212")
    monkeypatch.setenv("N_KB_MCP_ALLOWED_ORIGINS", "http://nkb.localhost,http://nkb.localhost:8212")

    settings = Settings(_env_file=None)

    assert settings.sqlite_path == Path("locals/test.db")
    assert settings.storage_root == Path("tmp/storage")
    assert settings.qdrant_url == "http://qdrant:6333"
    assert settings.qdrant_collection == "test_collection"
    assert settings.embedding_base_url == "http://ollama:11434"
    assert settings.embedding_model == "test-model"
    assert settings.ingestion_batch_size == 32
    assert settings.max_upload_bytes == 4096
    assert settings.allowed_file_extensions == {".md", ".txt"}
    assert settings.mcp_enabled is True
    assert settings.mcp_path == "/kb-mcp"
    assert settings.mcp_name == "Custom MCP"
    assert settings.mcp_allowed_hosts == ["nkb.localhost", "nkb.localhost:8212"]
    assert settings.mcp_allowed_origins == ["http://nkb.localhost", "http://nkb.localhost:8212"]


# --- OTel / profiling 配置（观测能力） ---

OTEL_PROFILING_ENV_VARS = (
    "N_KB_OTEL_ENABLED",
    "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT",
    "N_KB_OTEL_TRACES_SAMPLE_RATIO",
    "N_KB_OTEL_CONTENT_MAX_BYTES",
    "N_KB_OTEL_GRAFANA_BASE_URL",
    "N_KB_OTEL_GRAFANA_TRACES_BASE_URL",
    "N_KB_PROFILING_ENABLED",
    "N_KB_PROFILING_PYROSCOPE_ENDPOINT",
    "N_KB_PROFILING_INTERVAL_SECONDS",
)

ENDPOINT_ENV_VARS = {
    "otel_exporter_otlp_endpoint": "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT",
    "profiling_pyroscope_endpoint": "N_KB_PROFILING_PYROSCOPE_ENDPOINT",
    "otel_grafana_base_url": "N_KB_OTEL_GRAFANA_BASE_URL",
    "otel_grafana_traces_base_url": "N_KB_OTEL_GRAFANA_TRACES_BASE_URL",
}

INVALID_ENDPOINTS = [
    "ftp://collector:4317",
    "collector:4317",
    "http://",
    "http:///v1/traces",
    "http://user:pass@collector:4317",
    "http://@collector:4317",
    "http://collector:4317/?token=secret",
    "http://collector:4317/#frag",
    "http://collector:notaport",
    "http://collector:70000",
    "http://collector:0",
    "http://collector:-1",
]

VALID_ENDPOINTS = [
    "http://collector",
    "http://collector:4317",
    "https://collector.example.com",
    "https://collector.example.com:4317",
    "http://collector:4317/v1/traces",
    "http://[::1]:4317",
]

def _clear_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in OTEL_PROFILING_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def _settings_with(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Settings:
    _clear_otel_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def _error_messages(excinfo: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    """pydantic 的 str(e) 会回显 input_value，安全断言只能针对 msg（本仓校验器可控部分）。"""
    return [str(err.get("msg", "")) for err in excinfo.value.errors()]


def test_settings_otel_and_profiling_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_otel_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.otel_enabled is False
    assert settings.otel_exporter_otlp_endpoint == ""
    assert settings.otel_traces_sample_ratio == 1.0
    assert settings.otel_content_max_bytes == 2048
    assert settings.otel_grafana_base_url == ""
    assert settings.otel_grafana_traces_base_url == ""
    assert settings.profiling_enabled is False
    assert settings.profiling_pyroscope_endpoint == ""
    assert settings.profiling_interval_seconds == 60


def test_settings_reads_otel_and_profiling_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with(
        monkeypatch,
        {
            "N_KB_OTEL_ENABLED": "true",
            "N_KB_OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4317",
            "N_KB_OTEL_TRACES_SAMPLE_RATIO": "0.25",
            "N_KB_OTEL_CONTENT_MAX_BYTES": "4096",
            "N_KB_OTEL_GRAFANA_BASE_URL": "http://grafana:3000",
            "N_KB_OTEL_GRAFANA_TRACES_BASE_URL": "http://grafana:3000/explore",
            "N_KB_PROFILING_ENABLED": "true",
            "N_KB_PROFILING_PYROSCOPE_ENDPOINT": "http://pyroscope:4040",
            "N_KB_PROFILING_INTERVAL_SECONDS": "30",
        },
    )

    assert settings.otel_enabled is True
    assert settings.otel_exporter_otlp_endpoint == "http://otel-collector:4317"
    assert settings.otel_traces_sample_ratio == 0.25
    assert settings.otel_content_max_bytes == 4096
    assert settings.otel_grafana_base_url == "http://grafana:3000"
    assert settings.otel_grafana_traces_base_url == "http://grafana:3000/explore"
    assert settings.profiling_enabled is True
    assert settings.profiling_pyroscope_endpoint == "http://pyroscope:4040"
    assert settings.profiling_interval_seconds == 30


@pytest.mark.parametrize(
    "value",
    ["nan", "NaN", "inf", "-inf", "Infinity", "-Infinity", "1e400", "-1e400", "-0.1", "1.01", "2"],
)
def test_traces_sample_ratio_rejects_non_finite_and_out_of_range(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(monkeypatch, {"N_KB_OTEL_TRACES_SAMPLE_RATIO": value})

    assert any(
        "otel_traces_sample_ratio" in message for message in _error_messages(excinfo)
    )


@pytest.mark.parametrize(
    "value,expected",
    [("0", 0.0), ("0.25", 0.25), ("1", 1.0), ("1.0", 1.0)],
)
def test_traces_sample_ratio_accepts_bounds(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: float
) -> None:
    settings = _settings_with(monkeypatch, {"N_KB_OTEL_TRACES_SAMPLE_RATIO": value})

    assert settings.otel_traces_sample_ratio == expected


@pytest.mark.parametrize("value", ["0", "-1", "65537", "999999"])
def test_content_max_bytes_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(monkeypatch, {"N_KB_OTEL_CONTENT_MAX_BYTES": value})

    assert any(
        "otel_content_max_bytes" in message for message in _error_messages(excinfo)
    )


@pytest.mark.parametrize("value,expected", [("1", 1), ("2048", 2048), ("65536", 65536)])
def test_content_max_bytes_accepts_bounds(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    settings = _settings_with(monkeypatch, {"N_KB_OTEL_CONTENT_MAX_BYTES": value})

    assert settings.otel_content_max_bytes == expected


@pytest.mark.parametrize("value", ["9", "301", "0"])
def test_profiling_interval_seconds_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(monkeypatch, {"N_KB_PROFILING_INTERVAL_SECONDS": value})

    assert any(
        "profiling_interval_seconds" in message for message in _error_messages(excinfo)
    )


@pytest.mark.parametrize("value,expected", [("10", 10), ("60", 60), ("300", 300)])
def test_profiling_interval_seconds_accepts_bounds(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    settings = _settings_with(monkeypatch, {"N_KB_PROFILING_INTERVAL_SECONDS": value})

    assert settings.profiling_interval_seconds == expected


def test_profiling_enabled_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(monkeypatch, {"N_KB_PROFILING_ENABLED": "true"})

    assert any(
        "profiling_pyroscope_endpoint" in message for message in _error_messages(excinfo)
    )


def test_profiling_enabled_with_endpoint_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with(
        monkeypatch,
        {
            "N_KB_PROFILING_ENABLED": "true",
            "N_KB_PROFILING_PYROSCOPE_ENDPOINT": "http://pyroscope:4040",
        },
    )

    assert settings.profiling_enabled is True


def test_profiling_disabled_does_not_require_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with(monkeypatch, {"N_KB_PROFILING_ENABLED": "false"})

    assert settings.profiling_enabled is False
    assert settings.profiling_pyroscope_endpoint == ""


@pytest.mark.parametrize("env_var", sorted(ENDPOINT_ENV_VARS.values()))
@pytest.mark.parametrize("value", INVALID_ENDPOINTS)
def test_endpoints_reject_invalid_urls(
    monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings_with(monkeypatch, {env_var: value})

    messages = _error_messages(excinfo)
    for message in messages:
        assert value not in message
        assert "secret" not in message


@pytest.mark.parametrize("env_var", sorted(ENDPOINT_ENV_VARS.values()))
@pytest.mark.parametrize("value", VALID_ENDPOINTS)
def test_endpoints_accept_valid_urls(
    monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
) -> None:
    settings = _settings_with(monkeypatch, {env_var: value})

    field = next(name for name, var in ENDPOINT_ENV_VARS.items() if var == env_var)
    assert getattr(settings, field) == value


def test_endpoint_error_messages_do_not_echo_url_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = "http://user:secret@collector:4317"

    for env_var in ENDPOINT_ENV_VARS.values():
        with pytest.raises(ValidationError) as excinfo:
            _settings_with(
                monkeypatch, {env_var: bad, "N_KB_PROFILING_ENABLED": "true"}
            )

        messages = _error_messages(excinfo)
        assert messages, f"expected validation error for {env_var}"
        for message in messages:
            assert bad not in message
            assert "secret" not in message
            assert "user:pass" not in message
            assert "collector" not in message
