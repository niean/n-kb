from pathlib import Path
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="N_KB_",
        env_file=".env",
        extra="ignore",
    )

    sqlite_path: Path = Path("locals/n-kb.db")
    storage_root: Path = Path("data")
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "n_kb_documents"
    embedding_base_url: str = "http://localhost:11434"
    embedding_model: str = "bge-m3"
    ingestion_batch_size: int = 16
    max_upload_bytes: int = 2 * 1024 * 1024
    allowed_file_extensions: Annotated[set[str], NoDecode] = {".md", ".txt"}
    mcp_enabled: bool = False
    mcp_path: str = "/mcp"
    mcp_name: str = "N-KB MCP"
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "nkb.localhost",
        "nkb.localhost:*",
        "n-kb",
        "n-kb:*",
    ]
    mcp_allowed_origins: Annotated[list[str], NoDecode] = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "http://nkb.localhost",
        "http://nkb.localhost:*",
        "http://n-kb",
        "http://n-kb:*",
    ]

    @field_validator("allowed_file_extensions", mode="before")
    @classmethod
    def parse_allowed_file_extensions(cls, value: Any) -> set[str] | list[str] | Any:
        if isinstance(value, str):
            return {item.strip() for item in value.split(",") if item.strip()}
        if isinstance(value, (list, set, tuple)):
            return {str(item).strip() for item in value if str(item).strip()}
        return value

    @field_validator("mcp_allowed_hosts", "mcp_allowed_origins", mode="before")
    @classmethod
    def parse_string_list(cls, value: Any) -> list[str] | Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, set, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return value
