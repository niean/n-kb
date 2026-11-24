import math
import re
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _is_local_endpoint_host(hostname: str) -> bool:
    """内网主机判定（http 明文仅允许本机/内网接入）：回环地址、localhost 及
    其子域、无点单标签名（容器网络服务名等，无法经公共 DNS 解析）；其余一律
    视为外部地址，必须使用 HTTPS。"""
    lowered = hostname.lower().strip("[]")
    if lowered == "::1" or lowered.startswith("127."):
        return True
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    # 无点单标签名：仅容器网络/本机 hosts 可解析，不属于外部地址
    return "." not in lowered


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
    # Embedding HTTP 读超时（秒）。默认 30s：keep_alive 常驻 + 启动预热已
    # 消除冷加载窗口，embed 计算毫秒级，30s 足够且故障快速失败。若禁用
    # 预热或 keep_alive（含 Ollama 侧自行卸载），需调大到覆盖冷加载时长
    # （bge-m3 实测 45~90s）。
    embedding_timeout_seconds: float = 30.0
    # Ollama keep_alive（/api/embed 请求体透传）："-1" 模型常驻内存不卸载，
    # 也可用 Ollama 时长字符串（如 "30m"）。默认 "-1" 消除默认 5 分钟卸载
    # 导致的空闲后冷加载；内存占用约 1.1GB（bge-m3）。
    embedding_keep_alive: str = "-1"
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

    # 评估采集配置（B 类启动期 env-only，N_KB_ 前缀）。
    # capture_enabled 仅控制新样本与五计数；context/corpus 维护、tombstone 登记
    # 始终进行。endpoint/token 缺失时不启动网络投递循环，样本留存待补送。
    evaluation_capture_enabled: bool = False
    # N-Agent 基址；可携带可选基路径（拼接 producer 路径时保留），空表示未配置
    evaluation_endpoint_url: str = ""
    # 与 N-Agent N_AGENT_EVALUATION_PRODUCER_TOKEN 配对；为空不启动发送循环
    evaluation_service_token: str = ""

    # 定价目录配置（B 类启动期 env-only，无运行时修改接口）。
    # 目录 JSON 路径；为空表示成本目录不可用（成本按 pricing_unavailable 处理）
    pricing_catalog_path: str = ""
    # 目录内容的预期 SHA-256（64 位十六进制小写，与目录 JCS 规范化内容核对）；
    # 为空按目录不可用处理
    pricing_catalog_expected_sha256: str = ""

    @field_validator("embedding_timeout_seconds")
    @classmethod
    def validate_embedding_timeout_seconds(cls, value: float) -> float:
        if not value > 0:
            raise ValueError("embedding_timeout_seconds must be greater than 0")
        return value

    @field_validator("pricing_catalog_expected_sha256")
    @classmethod
    def validate_pricing_catalog_expected_sha256(cls, value: str) -> str:
        # 非空时必须是 64 位十六进制小写（与目录内容的 SHA-256 核对格式一致）
        if value != "" and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(
                "pricing_catalog_expected_sha256 must be 64 lowercase hex chars"
            )
        return value

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

    @field_validator("evaluation_endpoint_url")
    @classmethod
    def validate_evaluation_endpoint_url(cls, value: str) -> str:
        # 非空 endpoint 必须是合法 http(s) 基址：禁 userinfo/query/fragment；
        # 外部地址（非本机回环/内网单标签名）必须使用 HTTPS（Bearer 凭据不得
        # 明文出网）。错误信息不回显原始 URL 或凭据。
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("evaluation_endpoint_url must be an http/https URL")
        if not parsed.hostname:
            raise ValueError("evaluation_endpoint_url must include a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("evaluation_endpoint_url must not contain userinfo")
        if parsed.query:
            raise ValueError("evaluation_endpoint_url must not contain a query")
        if parsed.fragment:
            raise ValueError("evaluation_endpoint_url must not contain a fragment")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError(
                "evaluation_endpoint_url port must be an integer between 1 and 65535"
            ) from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(
                "evaluation_endpoint_url port must be an integer between 1 and 65535"
            )
        if parsed.scheme == "http" and not _is_local_endpoint_host(parsed.hostname):
            raise ValueError(
                "evaluation_endpoint_url must use https for external hosts"
            )
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
