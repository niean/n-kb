"""ProducerClient infrastructure 测试：严格确认分型、受控错误码、禁跳转与 Retry-After 解析。

HTTP 层使用 httpx.MockTransport，不访问真实网络。覆盖：
- 仅 202 且 JSON.status=="accepted" 判 accepted；204/其他 2xx/不可解析均归 retry unknown_ack；
- 401/403 归 auth_failed 并透传接收端受控 code；409 封闭 code 透传、未知 code 归 conflict_unknown；
- 413/422/其他确定性 4xx 归 rejected；429/5xx 归 retry 并解析 Retry-After（秒数/HTTP-date/无效值/过去时间）；
- 3xx 禁止自动跳转且只发一次请求（Bearer 不离开配置端点）；超时/连接失败归 retry；
- endpoint 校验拒绝非法基址；错误结果不携带 token。
"""
from __future__ import annotations

import time
from email.utils import formatdate

import httpx
import pytest

from app.infrastructure.evaluation.producer_client import (
    KIND_ACCEPTED,
    KIND_AUTH_FAILED,
    KIND_REJECTED,
    KIND_RETRY,
    ProducerClient,
)

TOKEN = "secret-token"
ENDPOINT = "http://n-agent.test/api"
MESSAGES_URL = "http://n-agent.test/api/evaluation/producer/messages"


def make_client(handler):
    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProducerClient(ENDPOINT, TOKEN, client=inner)


def json_response(status, payload, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


def err(status, code, headers=None):
    return json_response(status, {"error": {"code": code, "message": "detailed message"}}, headers=headers)


def recorder(response_factory):
    """记录收到的请求并按工厂生成响应。"""
    requests = []

    def handler(request):
        requests.append(request)
        return response_factory(request)

    return handler, requests


# ---------------------------------------------------------------------------
# 严格确认与未知确认
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_acceptance_202_with_accepted_body():
    handler, _ = recorder(lambda request: json_response(202, {"status": "accepted", "sample_id": "s-1"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_ACCEPTED
    assert outcome.code is None
    assert outcome.body == {"status": "accepted", "sample_id": "s-1"}
    assert outcome.retry_after is None


@pytest.mark.asyncio
async def test_202_with_non_accepted_status_is_unknown_ack():
    handler, _ = recorder(lambda request: json_response(202, {"status": "ok"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "unknown_ack"


@pytest.mark.asyncio
async def test_204_is_unknown_ack():
    handler, _ = recorder(lambda request: httpx.Response(204))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "unknown_ack"


@pytest.mark.asyncio
async def test_other_2xx_with_accepted_body_is_unknown_ack():
    handler, _ = recorder(lambda request: json_response(200, {"status": "accepted", "sample_id": "s-1"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "unknown_ack"


@pytest.mark.asyncio
async def test_202_unparseable_body_is_unknown_ack():
    handler, _ = recorder(lambda request: httpx.Response(202, content=b"not-json"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "unknown_ack"


@pytest.mark.asyncio
async def test_202_non_object_json_is_unknown_ack():
    handler, _ = recorder(lambda request: httpx.Response(202, json=["accepted"]))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "unknown_ack"


# ---------------------------------------------------------------------------
# 认证失败与 409 分型
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_maps_auth_failed_with_controlled_code():
    handler, _ = recorder(lambda request: err(401, "invalid_credentials"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_AUTH_FAILED
    assert outcome.code == "invalid_credentials"


@pytest.mark.asyncio
async def test_403_maps_auth_failed_with_controlled_code():
    handler, _ = recorder(lambda request: err(403, "service_mismatch"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_AUTH_FAILED
    assert outcome.code == "service_mismatch"


@pytest.mark.asyncio
async def test_401_unparseable_body_uses_fallback_code():
    handler, _ = recorder(lambda request: httpx.Response(401, content=b"html"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_AUTH_FAILED
    assert outcome.code == "auth_failed"


@pytest.mark.asyncio
async def test_409_known_code_passthrough():
    handler, _ = recorder(lambda request: err(409, "event_conflict"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "event_conflict"


@pytest.mark.parametrize(
    "code", ["deletion_conflict", "stale_sequence"]
)
@pytest.mark.asyncio
async def test_409_other_known_codes_passthrough(code):
    handler, _ = recorder(lambda request: err(409, code))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == code


@pytest.mark.asyncio
async def test_409_unknown_code_becomes_conflict_unknown():
    handler, _ = recorder(lambda request: err(409, "future_new_code"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "conflict_unknown"


@pytest.mark.asyncio
async def test_409_unparseable_body_becomes_conflict_unknown():
    handler, _ = recorder(lambda request: httpx.Response(409, content=b"oops"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "conflict_unknown"


# ---------------------------------------------------------------------------
# 413/422 与其他确定性 4xx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"), [(413, "payload_too_large"), (422, "protocol_invalid")]
)
@pytest.mark.asyncio
async def test_size_and_protocol_rejections_map_rejected(status, code):
    handler, _ = recorder(lambda request: err(status, code))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == code


@pytest.mark.asyncio
async def test_other_4xx_with_code_passthrough():
    handler, _ = recorder(lambda request: err(400, "bad_request"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "bad_request"


@pytest.mark.asyncio
async def test_other_4xx_without_code_uses_fallback():
    handler, _ = recorder(lambda request: httpx.Response(404, content=b"missing"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "rejected_4xx"


# ---------------------------------------------------------------------------
# 429/5xx 与 Retry-After 解析
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_retry_after_seconds_value():
    handler, _ = recorder(lambda request: err(429, "rate_limited", headers={"Retry-After": "120"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "rate_limited"
    assert outcome.retry_after == 120.0


@pytest.mark.asyncio
async def test_429_retry_after_http_date():
    http_date = formatdate(time.time() + 400, usegmt=True)
    handler, _ = recorder(lambda request: err(429, "rate_limited", headers={"Retry-After": http_date}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.retry_after == pytest.approx(400, abs=5)


@pytest.mark.asyncio
async def test_429_retry_after_invalid_value_ignored():
    handler, _ = recorder(lambda request: err(429, "rate_limited", headers={"Retry-After": "soon"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.retry_after is None


@pytest.mark.asyncio
async def test_429_retry_after_negative_seconds_ignored():
    handler, _ = recorder(lambda request: err(429, "rate_limited", headers={"Retry-After": "-5"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.retry_after is None


@pytest.mark.asyncio
async def test_429_retry_after_past_http_date_ignored():
    http_date = formatdate(time.time() - 100, usegmt=True)
    handler, _ = recorder(lambda request: err(429, "rate_limited", headers={"Retry-After": http_date}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.retry_after is None


def test_parse_retry_after_non_ascii_digits_ignored():
    # 全角数字 isdigit() 为 True 但 int() 抛 ValueError：必须走无效值路径而非异常
    # （httpx 层 header 值无法携带非 ASCII 字符，直接测解析函数）
    from app.infrastructure.evaluation.producer_client import _parse_retry_after

    assert _parse_retry_after("２０") is None
    assert _parse_retry_after(" １２ ") is None


def test_parse_retry_after_http_date_without_timezone_parsed():
    # 无时区 HTTP-date 按 UTC 解析（未来日期 → 正等待秒数）
    from app.infrastructure.evaluation.producer_client import _parse_retry_after

    retry_after = _parse_retry_after("Fri, 01 Jan 2038 00:00:00")
    assert retry_after is not None
    assert retry_after > 0


@pytest.mark.asyncio
async def test_5xx_maps_retry_without_retry_after():
    handler, _ = recorder(lambda request: err(500, "producer_ingest_failed"))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "remote_server_error"
    assert outcome.retry_after is None


@pytest.mark.asyncio
async def test_503_with_retry_after_seconds_honored():
    handler, _ = recorder(lambda request: err(503, "capacity_full", headers={"Retry-After": "30"}))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "remote_server_error"
    assert outcome.retry_after == 30.0


@pytest.mark.asyncio
async def test_429_unparseable_body_uses_fallback_code():
    handler, _ = recorder(lambda request: httpx.Response(429))
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "rate_limited"


# ---------------------------------------------------------------------------
# 3xx 禁止跳转、请求形状与网络故障
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_not_followed_and_single_request():
    def factory(request):
        return httpx.Response(302, headers={"Location": "http://evil.test/steal"})

    handler, requests = recorder(factory)
    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_REJECTED
    assert outcome.code == "redirect_not_followed"
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(MESSAGES_URL)


@pytest.mark.asyncio
async def test_sends_bearer_json_to_endpoint_joined_path():
    def factory(request):
        assert request.url == httpx.URL(MESSAGES_URL)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["content-type"] == "application/json"
        assert request.content == b'{"type":"evaluation.sample.v1"}'
        return json_response(202, {"status": "accepted", "sample_id": "s-1"})

    handler, requests = recorder(factory)
    outcome = await make_client(handler).send(b'{"type":"evaluation.sample.v1"}')

    assert outcome.kind == KIND_ACCEPTED
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_endpoint_trailing_slash_normalized():
    def factory(request):
        assert request.url == httpx.URL(MESSAGES_URL)
        return httpx.Response(204)

    client = ProducerClient("http://n-agent.test/api/", TOKEN, client=httpx.AsyncClient(
        transport=httpx.MockTransport(factory)
    ))
    await client.send(b"{}")


@pytest.mark.asyncio
async def test_timeout_maps_retry():
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "timeout"


@pytest.mark.asyncio
async def test_connect_error_maps_retry():
    def handler(request):
        raise httpx.ConnectError("refused")

    outcome = await make_client(handler).send(b"{}")

    assert outcome.kind == KIND_RETRY
    assert outcome.code == "network_error"


# ---------------------------------------------------------------------------
# endpoint 校验与错误信息受控
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://n-agent.test",
        "http://user:pass@n-agent.test/api",
        "http://n-agent.test/api?query=1",
        "http://n-agent.test/api#fragment",
        "not-a-url",
        "",
    ],
)
def test_endpoint_validation_rejects_invalid_base(endpoint):
    with pytest.raises(ValueError):
        ProducerClient(endpoint, TOKEN)


@pytest.mark.asyncio
async def test_outcome_never_carries_token():
    handler, _ = recorder(lambda request: err(401, "invalid_credentials"))
    outcome = await make_client(handler).send(b"{}")

    exported = {outcome.kind, str(outcome.code), str(outcome.retry_after), repr(outcome.body)}
    assert all(TOKEN not in value for value in exported)


@pytest.mark.asyncio
async def test_owned_client_aclose_is_safe():
    client = ProducerClient(ENDPOINT, TOKEN)
    await client.aclose()
    await client.aclose()
