import math
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
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
    # 观测配置：全部为启动期 env-only（N_KB_ 前缀），不提供运行时修改接口。
    # https 端点启用 TLS；http 端点仅用于明确内网接入（如 compose 内 collector）。
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    otel_traces_sample_ratio: float = 1.0
    otel_content_max_bytes: int = 1024000
    otel_grafana_base_url: str = ""
    otel_grafana_traces_base_url: str = ""
    otel_grafana_metrics_base_url: str = ""
    otel_grafana_logs_base_url: str = ""
    otel_grafana_profile_base_url: str = ""
    profiling_enabled: bool = False
    profiling_pyroscope_endpoint: str = ""
    profiling_interval_seconds: int = 60

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

    @field_validator(
        "otel_exporter_otlp_endpoint",
        "profiling_pyroscope_endpoint",
    )
    @classmethod
    def validate_http_urls(cls, value: str) -> str:
        # 非空 endpoint / Grafana base URL 必须是合法 http/https URL；
        # 错误信息不回显原始 URL 或凭据。
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("endpoint must be an http/https URL")
        if not parsed.hostname:
            raise ValueError("endpoint must include a hostname")
        if parsed.username is not None:
            raise ValueError("endpoint must not contain userinfo")
        if parsed.query:
            raise ValueError("endpoint must not contain a query")
        if parsed.fragment:
            raise ValueError("endpoint must not contain a fragment")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError(
                "endpoint port must be an integer between 1 and 65535"
            ) from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("endpoint port must be an integer between 1 and 65535")
        return value

    @field_validator("otel_grafana_base_url", "otel_grafana_traces_base_url", "otel_grafana_metrics_base_url", "otel_grafana_logs_base_url", "otel_grafana_profile_base_url")
    @classmethod
    def validate_grafana_urls(cls, value: str) -> str:
        if not value:
            return value
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(value)
            if any(ord(c) < 32 or ord(c) == 127 for c in value) or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
                raise ValueError()
            from urllib.parse import parse_qsl
            import re
            if parsed.fragment or any(re.search(r"token|password|secret|api.?key|credential", key, re.I) for key, _ in parse_qsl(parsed.query)):
                raise ValueError()
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError("Grafana URL must be http/https with a host and no userinfo or control characters") from None
        return value

    @field_validator("otel_traces_sample_ratio")
    @classmethod
    def validate_traces_sample_ratio(cls, value: float) -> float:
        if math.isnan(value) or math.isinf(value):
            raise ValueError(
                "otel_traces_sample_ratio must be between 0.0 and 1.0, not NaN or Infinity"
            )
        if not 0.0 <= value <= 1.0:
            raise ValueError("otel_traces_sample_ratio must be between 0.0 and 1.0")
        return value

    @field_validator("otel_content_max_bytes")
    @classmethod
    def validate_content_max_bytes(cls, value: int) -> int:
        if not 1 <= value <= 1024000:
            raise ValueError("otel_content_max_bytes must be between 1 and 1024000")
        return value

    @field_validator("profiling_interval_seconds")
    @classmethod
    def validate_profiling_interval_seconds(cls, value: int) -> int:
        if not 10 <= value <= 300:
            raise ValueError("profiling_interval_seconds must be between 10 and 300")
        return value

    @model_validator(mode="after")
    def validate_profiling_configuration(self) -> "Settings":
        # 总开关关闭时不要求 endpoint，也不启动任何 Profile 工作。
        if self.profiling_enabled and not self.profiling_pyroscope_endpoint:
            raise ValueError(
                "profiling_enabled=true requires a non-empty profiling_pyroscope_endpoint"
            )
        return self
