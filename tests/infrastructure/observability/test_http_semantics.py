import asyncio

import httpx
import pytest
from opentelemetry import trace

from app.infrastructure.observability import instrumentors as ins


@pytest.mark.parametrize("status,error,complete,expected", [
    (200, None, True, "success"), (422, None, True, "failure"),
    (500, None, True, "failure"), (None, None, False, "unknown"),
    (200, None, False, "unknown"), (None, TimeoutError(), False, "timed_out"),
    (None, httpx.ReadTimeout("x"), False, "timed_out"),
    (None, asyncio.CancelledError(), False, "cancelled"),
    (None, GeneratorExit(), False, "cancelled"),
])
def test_http_results_follow_real_transfer(status, error, complete, expected):
    span = ins._FilteringSpan(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), http_attributes={}, http_client=True)
    span._lifecycle = ins._ClientLifecycle(span)
    span._lifecycle.finish(complete=complete, error=error)
    assert span._outcome(status) == expected


def test_failed_send_does_not_record_untransmitted_response():
    span = ins._FilteringSpan(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), http_attributes={}, http_server=True)
    async def send(message):
        raise RuntimeError("disconnected")
    observer = ins._ObserveSend(send, span, True, 0)
    with pytest.raises(RuntimeError):
        asyncio.run(observer({"type": "http.response.start", "status": 201}))
    assert observer.status is None
    assert "http.response.status_code" not in span._http_attributes


@pytest.mark.parametrize("key,value", [
    ("http.response.status_code", True), ("http.response.status_code", 999),
    ("http.request.method", "credential"), ("server.port", -1),
    ("http.route", 123), ("db.system", False),
])
def test_standard_auto_attribute_types_are_strict(key, value):
    assert key not in ins._filter_attributes({key: value})


def test_auto_span_creation_failure_is_nonrecording():
    class BrokenTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("SDK unavailable")
    span = ins._FilteringTracer(BrokenTracer()).start_span("POST /retrieval/search", kind=trace.SpanKind.SERVER)
    assert not span.is_recording()


def test_wrapped_timeout_keeps_original_transfer_exception():
    original = RuntimeError("provider failed")
    original.__cause__ = httpx.ReadTimeout("sensitive detail")
    span = ins._FilteringSpan(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), http_attributes={}, http_client=True)
    lifecycle = ins._ClientLifecycle(span)
    span._lifecycle = lifecycle
    lifecycle.finish(error=original)
    assert lifecycle.error is original
    assert span._outcome(None) == "timed_out"


def test_request_observer_failure_does_not_change_application(monkeypatch):
    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})
    def broken(channel):
        raise RuntimeError("observer unavailable")
    monkeypatch.setattr(ins, "_request_scope_factory", broken)
    messages = []
    async def send(message):
        messages.append(message)
    asyncio.run(ins._HttpAccessFactsMiddleware(application)({"type": "http", "method": "GET", "path": "/documents"}, None, send))
    assert messages[0]["status"] == 204


@pytest.mark.parametrize("mode,expected", [("timeout", "timed_out"), ("wrapped_timeout", "timed_out"), ("denied", "denied")])
def test_sdk_failure_fallback_preserves_true_completion_outcome(monkeypatch, mode, expected):
    from app.application import telemetry_requests
    class BrokenTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("SDK failed")
    span = ins._FilteringTracer(BrokenTracer()).start_span("POST /retrieval/search", kind=trace.SpanKind.SERVER)
    monkeypatch.setattr(ins, "_request_scope_factory", telemetry_requests.request_scope)
    monkeypatch.setattr(ins, "_request_snapshot", telemetry_requests.current_request_facts)
    calls = []
    monkeypatch.setattr(ins.obs_logging, "log_http_request_completed", lambda *args, **kwargs: calls.append((args, kwargs)))
    error = TimeoutError()
    if mode == "wrapped_timeout":
        error = RuntimeError("wrapped")
        error.__cause__ = httpx.ConnectTimeout("unavailable")
    async def application(scope, receive, send):
        if mode == "denied":
            telemetry_requests.record_result("denied")
            await send({"type": "http.response.start", "status": 400})
            await send({"type": "http.response.body", "body": b""})
        else:
            raise error
    async def send(message):
        pass
    with trace.use_span(span, end_on_exit=False):
        if mode == "denied":
            asyncio.run(ins._HttpAccessFactsMiddleware(application)({"type": "http", "method": "POST", "path": "/retrieval/search"}, None, send))
        else:
            with pytest.raises(type(error)) as caught:
                asyncio.run(ins._HttpAccessFactsMiddleware(application)({"type": "http", "method": "POST", "path": "/retrieval/search"}, None, send))
            assert caught.value is error
    assert len(calls) == 1
    assert calls[0][0][-1] == expected
    if mode != "denied":
        assert calls[0][0][2] is None
