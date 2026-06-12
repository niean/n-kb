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
