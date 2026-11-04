"""T3 采集前安全投影测试：精确白名单、脱敏后限长、有界迭代与总量预算。

覆盖计划要求用例：前缀搭车、嵌套深度、宽记录、Unicode（“密”×5000 = 15000
字节）、凭据哨兵与短正文拒绝。截断验证一律使用允许的安全字段，不把正文类
字段当作可恢复内容。
"""
import json

import pytest

from app.infrastructure.observability.sanitizer import (
    FIELDS,
    bind_details,
    LogSanitizer,
    configure_details,
    flatten_details,
    project_details,
    validate_details,
)


@pytest.fixture(autouse=True)
def setup_details():
    from app.application import telemetry_details as d
    bind_details(configure=d.configure_details, project=d.project_details, validate=d.validate_details, flatten=d.flatten_details)
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({"super-secret-value"})
    configure_details(sanitizer.sanitize)
    yield
    configure_details(None)


# ---------------------------------------------------------------------------
# 精确键白名单（P058：无前缀放行）
# ---------------------------------------------------------------------------


def test_fields_whitelist_is_exact_enum():
    assert FIELDS == frozenset({
        "retrieval_input", "retrieval_output", "document_input", "index_input",
        "index_result", "embedding_input", "embedding_result",
        "http_request", "http_response", "error", "log_fields",
    })


def test_content_keys_rejected_even_when_tiny():
    assert project_details(
        body="x", query="q", snippet="s", document_text="d",
        embedding="e", vector="v", source_uri="u", tags={"a": "b"},
    ) == {}


def test_prefix_smuggling_keys_dropped():
    assert project_details(
        error_detail="x", errors="x", log_fields_body="x",
        http_requests={}, stage_results_extra={}, counts_override={},
        http_request_body={}, target_url="x",
    ) == {}


def test_nested_content_keys_redacted_regardless_of_length():
    value = {"Body": "tiny", "query": "q", "snippet": "s", "note": "kept"}
    content = project_details(log_fields=value)["log_fields"]["content"]
    parsed = json.loads(content)
    assert parsed["Body"] == "tiny"
    assert parsed["query"] == "q"
    assert parsed["snippet"] == "s"
    assert parsed["note"] == "kept"
    # 输入不被原地修改。
    assert value == {"Body": "tiny", "query": "q", "snippet": "s", "note": "kept"}


# ---------------------------------------------------------------------------
# 凭据先遮盖 + 哨兵
# ---------------------------------------------------------------------------


def test_credential_keys_masked_and_catalog_sentinel_absent():
    value = {
        "api_key": "super-secret-value",
        "nested": {"Authorization": "Bearer private-bearer-token", "token": "abc"},
        "note": "password=hunter2",
    }
    content = project_details(log_fields=value)["log_fields"]["content"]
    for sentinel in ("super-secret-value", "private-bearer-token", "abc", "hunter2"):
        assert sentinel not in content
    parsed = json.loads(content)
    assert parsed["api_key"] == "[REDACTED]"
    assert parsed["nested"]["Authorization"] == "[REDACTED]"
    assert parsed["nested"]["token"] == "[REDACTED]"
    assert "[REDACTED]" in parsed["note"]
    assert value["api_key"] == "super-secret-value"


def test_credential_masking_happens_before_truncation():
    configure_details(LogSanitizer().sanitize, 32)
    # 凭据值先整体遮盖；遮盖后剩余文本仍超限时再按 UTF-8 字节截断。
    item = project_details(error="api_key=short padding-after " + "x" * 100)["error"]
    assert item["truncated"] is True
    assert item["content"].startswith("api_key=[REDACTED]")
    assert "short" not in item["content"]
    assert len(item["content"].encode("utf-8")) <= 32
    assert item["original_bytes"] > len(item["content"].encode("utf-8"))


def test_primitive_catalog_secret_redacted_without_changing_normal_types():
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({"123456", "true"})
    configure_details(sanitizer.sanitize)
    assert project_details(error=123456)["error"]["content"] == "[REDACTED]"
    # 目录值对 JSON 文本同样生效（对齐 N-Agent：'true' 在目录内即被遮盖）。
    parsed = json.loads(project_details(
        log_fields={"normal": 42, "flag": True, "empty": None, "ratio": 1.5}
    )["log_fields"]["content"])
    assert parsed == {"normal": 42, "flag": "[REDACTED]", "empty": None, "ratio": 1.5}


# ---------------------------------------------------------------------------
# 先脱敏、再限长（UTF-8 字节边界，不破坏码点）
# ---------------------------------------------------------------------------


def test_sanitize_before_truncate_utf8_no_codepoint_break():
    configure_details(lambda s: s.replace("secret", "安全"), 7, max_total_bytes=512)
    item = project_details(error="secret😀中文")["error"]
    assert item == {"content": "安全", "original_bytes": 16, "truncated": True}
    configure_details(lambda s: s, 1, max_total_bytes=512)
    assert project_details(error="😀")["error"]["content"] == ""


def test_cjk_5000_chars_is_15000_bytes():
    configure_details(LogSanitizer().sanitize, 2048)
    item = project_details(error="密" * 5000)["error"]
    assert item["original_bytes"] == 15000
    assert item["truncated"] is True
    raw = item["content"].encode("utf-8")
    assert len(raw) <= 2048
    assert len(raw) % 3 == 0  # 没有截断出半个码点
    assert item["content"] == "密" * (len(raw) // 3)


def test_truncation_uses_allowed_safe_field_not_body():
    configure_details(lambda s: s, 64)
    item = project_details(index_result="阶段" * 100)["index_result"]
    assert item["truncated"] is True
    assert len(item["content"].encode("utf-8")) <= 64
    item["content"].encode("utf-8").decode("utf-8")  # 内容仍是合法 UTF-8


# ---------------------------------------------------------------------------
# 有界遍历：深度 / 字段数 / 列表项数 / 总量预算
# ---------------------------------------------------------------------------


def test_nesting_depth_bounded_fail_closed():
    deep = current = {}
    for _ in range(100):
        current["child"] = {}
        current = current["child"]
    current["leaf"] = "value"
    assert project_details(log_fields=deep)["log_fields"]["incomplete"] is True


def test_wide_dict_and_long_list_capped():
    wide = {f"key{i}": i for i in range(5000)}
    parsed = json.loads(project_details(log_fields=wide)["log_fields"]["content"])
    named = [k for k in parsed if k != "[truncated]"]
    assert len(named) <= 128
    assert "[truncated]" in parsed
    assert "key4999" not in parsed

    wrapped = {"items": list(range(5000)), "name": "ok"}
    parsed2 = json.loads(project_details(log_fields=wrapped)["log_fields"]["content"])
    assert len(parsed2["items"]) <= 129
    assert "[truncated]" in parsed2["items"]
    assert parsed2["name"] == "ok"


def test_total_record_budget_flags_remaining_fields():
    configure_details(lambda s: s, 64, max_total_bytes=256)
    result = project_details(error="b" * 100, retrieval_input="a" * 100,
                             log_fields={"k": "v"}, counts={"n": 1})
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 256
    assert result["error"]["truncated"] is True
    assert result.details_unavailable is True


# ---------------------------------------------------------------------------
# 拒绝危险值：非有限浮点、未知对象、媒体与字节
# ---------------------------------------------------------------------------


def test_non_finite_floats_rejected_fail_closed():
    assert project_details(counts={"ratio": float("nan")}) == {}
    assert project_details(error=float("inf")) == {}
    assert project_details(log_fields={"ratio": float("-inf")}) == {}


def test_unknown_object_never_calls_str_or_repr():
    class Boom:
        def __str__(self):
            raise AssertionError("str() must not be called on unknown objects")

        def __repr__(self):
            raise AssertionError("repr() must not be called on unknown objects")

    parsed = json.loads(
        project_details(log_fields={"obj": Boom()})["log_fields"]["content"]
    )
    assert parsed["obj"] == "[unsupported value: Boom]"


def test_bytes_and_data_media_redacted():
    content = project_details(log_fields={
        "blob": b"raw-upload-bytes",
        "inline": "data:image/png;base64,AAAA",
    })["log_fields"]["content"]
    assert "raw-upload-bytes" not in content
    assert "AAAA" not in content


def test_json_string_value_reprojected_with_masking():
    content = project_details(log_fields={
        "payload_json": '{"Authorization": "private-bearer-value"}',
    })["log_fields"]["content"]
    assert "private-bearer-value" not in content


def test_non_string_dict_keys_projected():
    parsed = json.loads(project_details(
        log_fields={42: "v", None: "n", 1.5: "f"}
    )["log_fields"]["content"])
    assert parsed == {"42": "v", "null": "n", "1.5": "f"}


# ---------------------------------------------------------------------------
# fail closed：sanitizer 缺失或违反合同
# ---------------------------------------------------------------------------


def test_fail_closed_on_missing_or_broken_sanitizer():
    configure_details(None)
    assert project_details(error="private") == {}
    configure_details(lambda s: None)
    assert project_details(error="private") == {}
    configure_details(lambda s: 1)
    assert project_details(error="private") == {}

    def broken(s):
        raise ValueError("boom")

    configure_details(broken)
    assert project_details(error="private") == {}


def test_configure_details_validates_arguments():
    with pytest.raises(ValueError):
        configure_details(lambda s: s, 0)
    with pytest.raises(ValueError):
        configure_details(lambda s: s, True)
    with pytest.raises(ValueError):
        configure_details(lambda s: s, 2048, max_total_bytes=0)
    with pytest.raises(ValueError):
        configure_details(lambda s: s, 2048, max_total_bytes=True)


# ---------------------------------------------------------------------------
# validate_details / flatten_details（出口合同）
# ---------------------------------------------------------------------------


def test_validate_details_rejects_unknown_fields_and_extra_metadata():
    details = project_details(error="safe")
    assert details["error"]["content"] == "safe"
    details["other"] = {"content": "private"}
    details["error"]["injected"] = "private"
    assert validate_details(details) == {}
    good = {"error": {"content": "safe", "original_bytes": 4, "truncated": False}}
    assert validate_details(good) == good
    assert validate_details(
        {"error": {"content": "x", "original_bytes": -1, "truncated": False}}
    ) == {}
    assert validate_details("not-a-dict") == {}


def test_flatten_details_uses_nkb_prefix_and_drops_none():
    flat = flatten_details({"error": {"content": "x", "original_bytes": None,
                                      "truncated": True, "incomplete": True,
                                      "observed_bytes": 99}})
    assert flat["nkb.content.error_observed_bytes"] == 99
    assert flat["nkb.content.error"] == "x"
    assert "nkb.content.error_original_bytes" not in flat
    assert flatten_details(
        {"unknown": {"content": "x", "original_bytes": 1, "truncated": False}}
    ) == {}


# ---------------------------------------------------------------------------
# LogSanitizer 固定模式 + 秘密目录（白名单之外的补充层）
# ---------------------------------------------------------------------------


def test_log_sanitizer_fixed_patterns_and_catalog():
    sanitizer = LogSanitizer()
    out = sanitizer.sanitize(
        "Bearer abc123token url http://user:pass@host.example/api "
        "api_key=xyz789secret "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
    )
    for sentinel in ("abc123token", "pass", "xyz789secret",
                     "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"):
        assert sentinel not in out
    assert "[REDACTED]" in out

    pem = "-----BEGIN PRIVATE KEY-----\npem-secret-123\n-----END PRIVATE KEY-----"
    assert "pem-secret-123" not in sanitizer.sanitize(pem)

    sanitizer.configure_secret_catalog({"split-catalog-secret"})
    redacted = sanitizer.sanitize("value split-catalog-secret end")
    assert "split-catalog-secret" not in redacted
    assert "[REDACTED]" in redacted
