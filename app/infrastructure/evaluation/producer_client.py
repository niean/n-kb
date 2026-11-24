# app/infrastructure/evaluation/producer_client.py
"""N-Agent producer 端点投递客户端（httpx 实现）。

职责边界：仅负责单次 HTTP 投递与响应粗分型——POST /evaluation/producer/messages，
Bearer 认证，10s 总超时，禁止 3xx 自动跟随（Bearer 不得离开配置端点）。返回结构化
DeliveryOutcome（kind/code/retry_after/body）：错误只携带受控 code（来自接收端
封闭错误契约或本模块回退码），绝不包含 token、响应正文或 query；解析成功的响应
body 仅供调用方做严格确认校验（sample_id/tombstone），不得进入任何持久化错误
信息或日志。

分型表（与接收端 app/interfaces/http/evaluation_routes.py 错误契约对齐）：
- 202 且 JSON.status=="accepted" -> accepted（消息类型级补充校验由 worker 负责）；
- 204/其他 2xx/不可解析 -> retry unknown_ack（未知确认，重试同字节）；
- 401/403 -> auth_failed（invalid_credentials/service_mismatch，缺失时回退 auth_failed）；
- 409 -> rejected，封闭 code（event_conflict/deletion_conflict/stale_sequence）透传，
  未知 code 统一归 conflict_unknown；
- 413/422 -> rejected（payload_too_large/protocol_invalid）；其他 4xx -> rejected
  （透传 code 或回退 rejected_4xx）；3xx -> rejected redirect_not_followed；
- 429/5xx -> retry（rate_limited/remote_server_error），有效 Retry-After 解析为
  retry_after 秒数；超时/连接故障 -> retry（timeout/network_error）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

# 投递结果分型（worker 按此四类决定处置）
KIND_ACCEPTED = "accepted"
KIND_REJECTED = "rejected"
KIND_RETRY = "retry"
KIND_AUTH_FAILED = "auth_failed"

# 接收端 producer 端点路径与 409 封闭 code 集合
PRODUCER_MESSAGES_PATH = "/evaluation/producer/messages"
KNOWN_CONFLICT_CODES = frozenset({"event_conflict", "deletion_conflict", "stale_sequence"})

# 受控回退 code：接收端 body 不可解析或不携带 code 时使用，不含任何动态正文
FALLBACK_AUTH_CODE = "auth_failed"
FALLBACK_CONFLICT_CODE = "conflict_unknown"
FALLBACK_REJECT_CODE = "rejected_4xx"
CODE_UNKNOWN_ACK = "unknown_ack"
CODE_REDIRECT_NOT_FOLLOWED = "redirect_not_followed"
CODE_PAYLOAD_TOO_LARGE = "payload_too_large"
CODE_PROTOCOL_INVALID = "protocol_invalid"
CODE_RATE_LIMITED = "rate_limited"
CODE_REMOTE_SERVER_ERROR = "remote_server_error"
CODE_TIMEOUT = "timeout"
CODE_NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class DeliveryOutcome:
    """单次投递结果：kind 四类分型；code 为受控错误码；retry_after 为服务端
    要求的等待秒数（仅 retry 分型可能携带）；body 为解析成功的 JSON 对象。"""

    kind: str
    code: str | None = None
    retry_after: float | None = None
    body: dict[str, Any] | None = None


def _validate_endpoint(endpoint: str) -> str:
    """校验 producer 基址：合法 http(s)、无 userinfo/query/fragment；允许携带
    可选基路径（拼接 messages 路径时保留）。"""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("producer endpoint 必须是合法 http(s) 基址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("producer endpoint 禁止 userinfo/query/fragment")
    return endpoint.rstrip("/")


def _parse_json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _error_code(response: httpx.Response) -> str | None:
    """提取接收端统一错误契约 {"error": {"code": ...}} 中的受控 code。"""
    body = _parse_json_object(response)
    if body is None:
        return None
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    return None


def _parse_retry_after(header_value: str | None, *, now_epoch: float | None = None) -> float | None:
    """解析 Retry-After：秒数或 HTTP-date（RFC 7231）；缺失/负数/过去时间/无法
    解析一律返回 None（不作为等待下限）。"""
    if not header_value:
        return None
    text = header_value.strip()
    # isascii 前置：全角等非 ASCII 数字 isdigit() 为 True 但 int() 抛 ValueError
    if text.isascii() and text.isdigit():
        seconds = int(text)
        return float(seconds) if seconds >= 0 else None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now_epoch if now_epoch is not None else time.time()
    delta = parsed.timestamp() - now
    return delta if delta > 0 else None


class ProducerClient:
    """评估投递端口实现：组合根注入 endpoint 与 token；token 仅来自启动期
    配置，本类不提供运行期修改入口。"""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint = _validate_endpoint(endpoint)
        self._token = token
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            # httpx.Timeout 为分阶段超时（connect/read/write/pool 各 10s），并非
            # 单请求总时限；按各阶段 10s 的惯用法即可接受，不追求真实总时限
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        """回收自建客户端（外部注入的客户端由注入方负责生命周期）。"""
        if self._owns_client:
            await self._client.aclose()

    async def send(self, message_bytes: bytes) -> DeliveryOutcome:
        """投递单条消息字节；网络/超时异常内部归入 retry 分型，不向外抛出。"""
        url = f"{self._endpoint}{PRODUCER_MESSAGES_PATH}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(
                url, content=message_bytes, headers=headers, follow_redirects=False
            )
        except httpx.TimeoutException:
            return DeliveryOutcome(kind=KIND_RETRY, code=CODE_TIMEOUT)
        except httpx.HTTPError:
            return DeliveryOutcome(kind=KIND_RETRY, code=CODE_NETWORK_ERROR)
        return self._classify_response(response)

    def _classify_response(self, response: httpx.Response) -> DeliveryOutcome:
        status = response.status_code
        if 300 <= status < 400:
            # 禁止自动跳转：Bearer 不发往重定向目标，报配置错误由调用方保留消息
            return DeliveryOutcome(kind=KIND_REJECTED, code=CODE_REDIRECT_NOT_FOLLOWED)
        if 200 <= status < 300:
            body = _parse_json_object(response)
            if status == 202 and body is not None and body.get("status") == "accepted":
                return DeliveryOutcome(kind=KIND_ACCEPTED, body=body)
            return DeliveryOutcome(kind=KIND_RETRY, code=CODE_UNKNOWN_ACK)
        error_code = _error_code(response)
        if status in (401, 403):
            return DeliveryOutcome(
                kind=KIND_AUTH_FAILED, code=error_code or FALLBACK_AUTH_CODE
            )
        if status == 409:
            code = error_code if error_code in KNOWN_CONFLICT_CODES else FALLBACK_CONFLICT_CODE
            return DeliveryOutcome(kind=KIND_REJECTED, code=code)
        if status == 413:
            return DeliveryOutcome(
                kind=KIND_REJECTED, code=error_code or CODE_PAYLOAD_TOO_LARGE
            )
        if status == 422:
            return DeliveryOutcome(
                kind=KIND_REJECTED, code=error_code or CODE_PROTOCOL_INVALID
            )
        if status == 429 or 500 <= status < 600:
            return DeliveryOutcome(
                kind=KIND_RETRY,
                code=CODE_RATE_LIMITED if status == 429 else CODE_REMOTE_SERVER_ERROR,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )
        if 400 <= status < 500:
            return DeliveryOutcome(
                kind=KIND_REJECTED, code=error_code or FALLBACK_REJECT_CODE
            )
        return DeliveryOutcome(kind=KIND_RETRY, code=CODE_UNKNOWN_ACK)
