import asyncio

from app.application import telemetry_requests as requests


def test_result_crosses_worker_thread_without_leaking_next_request():
    async def run():
        with requests.request_scope("api"):
            await asyncio.to_thread(requests.record_result, "success")
            facts = requests.current_request_facts()
            assert facts["nkb.request.outcome"] == "success"
            assert facts["nkb.request.duration_ms"] >= 0
        with requests.request_scope("mcp_http"):
            assert requests.current_request_facts() == {"nkb.request.channel": "mcp_http", "nkb.request.operation": "handle"}
        assert requests.current_request_facts() == {}
    asyncio.run(run())


def test_parallel_requests_and_nested_scope_restore_owner():
    async def worker(channel, outcome):
        with requests.request_scope(channel):
            with requests.request_scope("unknown"):
                requests.record_result("denied")
            await asyncio.sleep(0)
            requests.record_result(outcome)
            return requests.current_request_facts()
    async def run():
        a, b = await asyncio.gather(worker("api", "success"), worker("mcp_http", "failure"))
        assert a["nkb.request.outcome"] == "success"
        assert b["nkb.request.outcome"] == "failure"
        assert a["nkb.request.channel"] == "api"
        assert b["nkb.request.channel"] == "mcp_http"
    asyncio.run(run())
