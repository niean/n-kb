from pathlib import Path

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
