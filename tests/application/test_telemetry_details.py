import json
import asyncio
import pytest
from app.application import telemetry_details as d
from app.infrastructure.observability.sanitizer import LogSanitizer

@pytest.fixture(autouse=True)
def configured():
    s = LogSanitizer()
    s.configure_secret_catalog({"catalog-secret"})
    d.configure_details(s.sanitize)
    yield
    d.configure_details(None)

def test_readable_content_and_redacted_restricted_data():
    value = {"query":"where?", "text":"readable", "snippet":"piece", "password":"hidden", "tags":{"private":"hidden"}, "vector":[1,2], "file_content":"full"}
    item = d.project_details(retrieval_input=value)["retrieval_input"]
    parsed = json.loads(item["content"])
    assert parsed["query"] == "where?"
    assert parsed["text"] == "readable"
    assert "hidden" not in item["content"]
    assert parsed["vector"] == "[REDACTED]"
    assert value["password"] == "hidden"

@pytest.mark.parametrize("limit", [1,2048,65536])
def test_item_and_total_budget(limit):
    d.configure_details(LogSanitizer().sanitize, limit)
    result = d.project_details(**{name:"密" * 40000 for name in ("error","retrieval_input","retrieval_output","log_fields")})
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4*limit
    assert all(len(item["content"].encode()) <= limit for item in result.values())
    if limit == 1:
        assert result == {}
    else:
        assert "error" in result


def test_complete_size_and_incomplete_traversal():
    item = d.project_details(error="密"*5000)["error"]
    assert item["original_bytes"] == 15000
    assert item["truncated"]
    cyclic = {}; cyclic["self"] = cyclic
    item = d.project_details(log_fields=cyclic)["log_fields"]
    assert item["original_bytes"] is None
    assert item["incomplete"]
    assert "nkb.details.log_fields.original_bytes" not in d.flatten_details({"log_fields":item})


def test_forged_envelope_is_sanitized():
    forged = {"error":{"content":"password=hidden", "original_bytes":15,"truncated":False}}
    assert "hidden" not in json.dumps(d.validate_details(forged))


def test_scopes_are_updateable_copies_and_restore():
    assert d.current_details() == {}
    with d.detail_scope(retrieval_input={"query":"outer"}) as scope:
        snapshot = d.current_details(); snapshot.clear()
        assert d.current_details()
        with d.detail_scope(error="inner"):
            assert "error" in d.current_details()
        assert "error" not in d.current_details()
        scope.update(retrieval_output={"count":1})
        assert "retrieval_output" in d.current_details()
    assert d.current_details() == {}


def test_partial_credentials_fail_closed():
    s = LogSanitizer(); s.configure_secret_catalog({"catalog-secret"})
    for value in ("catalog-sec", '{"password":"hid', "password%3Dhid", "api_key=hid", "safe catalog-se"):
        out = s.sanitize_partial(value)
        assert "hid" not in out and "catalog-se" not in out


def test_flatten_preserves_known_complete_size_and_detects_mutation():
    item = d.project_details(error="x"*3000)
    assert d.flatten_details(item)["nkb.details.error.original_bytes"] == 3000
    item["error"]["content"] = "password=hidden"
    assert "hidden" not in json.dumps(d.validate_details(item))


def test_nested_json_and_legacy_mapping():
    result = d.project_details(stage="split", counts={"n":2}, sqlite={"operation":"select","table":"documents","sql":"SECRET SQL"}, target={"scheme":"http","host":"localhost","port":80}, log_fields={"nested":'{"vector":[1,2],"file_content":"WHOLE FILE","query":"read me"}'})
    assert "index_result" in result and "http_request" in result
    serialized = json.dumps(result)
    assert "SECRET SQL" not in serialized and "WHOLE FILE" not in serialized
    assert "read me" in serialized


def test_shared_traversal_and_scanning_budget():
    wide = [{str(k):"ok" for k in range(128)} for _ in range(128)]
    item = d.project_details(log_fields=wide)["log_fields"]
    assert item["incomplete"] and item["original_bytes"] is None
    item = d.project_details(error="catalog-secret" * 20000)["error"]
    assert item["incomplete"] and item["original_bytes"] is None
    assert "catalog-sec" not in item["content"]


def test_budget_helper_reserves_actual_envelope_bytes():
    result = d.project_details(error="x"*2000, retrieval_input="y"*2000)
    shrunk = d.budget_details(result, max_total_bytes=500)
    assert len(json.dumps(shrunk, ensure_ascii=False).encode()) <= 500
    assert shrunk["error"]["original_bytes"] == 2000
    assert shrunk["error"]["truncated"]


def test_async_and_thread_context_isolation():
    from concurrent.futures import ThreadPoolExecutor
    async def worker(name):
        with d.detail_scope(error=name):
            await asyncio.sleep(0)
            return d.current_details()["error"]["content"]
    async def run():
        return await asyncio.gather(worker("a"),worker("b"))
    assert asyncio.run(run()) == ["a","b"]
    with d.detail_scope(error="parent"):
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(d.current_details).result() == {}
    assert d.current_details() == {}


def test_outer_owner_update_preserves_active_child_snapshot():
    with d.detail_scope(error="parent") as owner:
        with d.detail_scope(error="child"):
            owner.update(retrieval_output="parent result")
            assert d.current_details()["error"]["content"] == "child"
        assert d.current_details()["retrieval_output"]["content"] == "parent result"


def test_invalid_unicode_nonfinite_and_hostile_objects_are_safe():
    class Hostile:
        def __str__(self):
            raise AssertionError("str called")
        def __repr__(self):
            raise AssertionError("repr called")
    assert d.project_details(error="\ud800") == {}
    assert d.project_details(error=float("nan")) == {}
    assert "Hostile" in d.project_details(error=Hostile())["error"]["content"]


def test_truncated_sensitive_key_never_exports_its_value():
    result = d.project_details(log_fields={"x"*256+"password":"PASSWORD_SENTINEL"})
    assert "PASSWORD_SENTINEL" not in json.dumps(result)
    assert result["log_fields"]["incomplete"] is True
    assert result["log_fields"]["original_bytes"] is None


def test_child_task_owner_updates_do_not_poison_later_parent_updates():
    async def run():
        with d.detail_scope(retrieval_input="parent") as owner:
            async def child():
                owner.update(retrieval_input="child")
                assert d.current_details()["retrieval_input"]["content"] == "child"
            await asyncio.create_task(child())
            assert d.current_details()["retrieval_input"]["content"] == "parent"
            owner.update(retrieval_output="parent result")
            assert d.current_details()["retrieval_input"]["content"] == "parent"
    asyncio.run(run())


def test_truncated_key_name_never_exports_known_secret_prefix():
    result = d.project_details(log_fields={"x"*250+"catalog-secret":"VALUE"})
    assert "catalo" not in json.dumps(result)
    assert result["log_fields"]["incomplete"] is True


def test_old_envelopes_respect_new_secret_catalog():
    sanitizer = LogSanitizer()
    d.configure_details(sanitizer.sanitize)
    result = d.project_details(error="later-secret")
    sanitizer.configure_secret_catalog({"later-secret"})
    assert "later-secret" not in json.dumps(d.flatten_details(result))


def test_old_envelopes_respect_smaller_content_limit():
    sanitizer = LogSanitizer()
    d.configure_details(sanitizer.sanitize)
    result = d.project_details(error="abcdefghij")
    d.configure_details(sanitizer.sanitize, max_bytes=1, max_total_bytes=1000)
    assert len(d.flatten_details(result)["nkb.details.error.content"].encode()) <= 1
