import json
import logging
import asyncio
import gzip
import zlib

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_details as details
from app.infrastructure.observability import logging as logs, sanitizer
from app.infrastructure.observability import instrumentors as ins


@pytest.fixture
def runtime():
    logs._reset_for_tests()
    safe = logs.prepare_sanitizer()
    safe.configure_secret_catalog(['http-secret-sentinel'])
    sanitizer.bind_details(configure=details.configure_details, project=details.project_details,
                           validate=details.validate_details, flatten=details.flatten_details)
    sanitizer.configure_details(safe.sanitize, 2048)
    logs.configure_execution_context(project=details.project_details, validate=details.validate_details,
        flatten=details.flatten_details, budget=details.budget_details, details_snapshot=details.current_details)
    logger = logging.getLogger(logs.__name__)
    level = logger.level
    logger.setLevel(logging.INFO)
    bridge = logs.SafeLogBridge()
    logger.addHandler(bridge)
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    logger.removeHandler(bridge)
    logger.setLevel(level)
    provider.shutdown()
    logs._reset_for_tests()
    sanitizer.configure_details(None)


def assert_exchange(span, request_text, response_text):
    attrs = dict(span.attributes)
    assert request_text in attrs['nkb.details.http_request.content']
    assert response_text in attrs['nkb.details.http_response.content']
    assert 'http-secret-sentinel' not in json.dumps(attrs)
    assert len(span.events) == 1
    for key in ('http_request', 'http_response'):
        name = 'nkb.details.' + key + '.content'
        assert span.events[0].attributes[name] == attrs[name]


def test_asgi_request_response_payloads_on_server_attributes_and_event(runtime):
    provider, exporter = runtime
    app = FastAPI()

    @app.post('/retrieval/search')
    async def search(request: Request):
        body = await request.json()
        return {'query': body['query'], 'results': [{'document_id':'doc', 'chunk_id':'chunk',
            'score': .9, 'snippet':'response-snippet', 'tags': {'private':'do-not-export'}}]}

    ins.instrument_app(app, tracer_provider=provider)
    with TestClient(app) as client:
        response = client.post('/retrieval/search', json={'query':'request-query http-secret-sentinel'})
    assert response.status_code == 200
    server = next(s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER)
    assert_exchange(server, 'request-query', 'response-snippet')


def test_httpx_consumed_stream_is_not_read_ahead_and_keeps_client_owner(runtime):
    provider, exporter = runtime
    reads = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b'{"answer":"', b'client-result http-secret-sentinel"}'):
                reads.append(chunk)
                yield chunk

    tracer = ins._FilteringTracer(provider.get_tracer('http-test'))
    request = httpx.Request('POST','http://downstream/retrieval/search',
        json={'query':'client-query http-secret-sentinel'})
    with provider.get_tracer('root').start_as_current_span('root'):
        with tracer.start_as_current_span('POST',kind=trace.SpanKind.CLIENT,
                attributes={'http.request.method':'POST','server.address':'downstream','url.template':'/retrieval/search'}):
            response = ins._sync_transport_wrapper(lambda request:httpx.Response(200,
                headers={'content-type':'application/json'},stream=Stream()),None,(request,),{})
            assert reads == []
        assert not any(s.kind == trace.SpanKind.CLIENT for s in exporter.get_finished_spans())
        assert b'client-result' in b''.join(response.iter_bytes())
        response.close()
    client = next(s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT)
    assert len(reads) == 2
    assert_exchange(client,'client-query','client-result')


def test_async_httpx_request_response_streams_preserve_bytes_and_owner(runtime):
    provider, exporter = runtime

    async def run():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"query":"async-response"}'
        request = httpx.Request('POST','http://downstream/retrieval/search',json={'query':'async-request'})
        async def transport(request):
            return httpx.Response(200, headers={'content-type':'application/json'}, stream=Stream())
        with provider.get_tracer('root').start_as_current_span('root'):
            tracer = ins._FilteringTracer(provider.get_tracer('async'))
            with tracer.start_as_current_span('POST', kind=trace.SpanKind.CLIENT):
                response = await ins._async_transport_wrapper(transport,None,(request,),{})
            assert await response.aread() == b'{"query":"async-response"}'
            await response.aclose()
    asyncio.run(run())
    client = next(s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT)
    assert_exchange(client,'async-request','async-response')


@pytest.mark.parametrize('encoding,compress',[('gzip',gzip.compress),('deflate',zlib.compress)])
def test_compressed_and_partial_bodies_never_leak_prefixes(runtime,encoding,compress):
    from app.infrastructure.observability.http_capture import Body
    body = Body({'content-type':'application/json','content-encoding':encoding})
    raw = compress(b'{"query":"readable"}')
    for byte in raw:
        body.append(bytes([byte]))
    body.complete = True
    assert body.value() == {'query':'readable'}
    assert body.observed == len(raw)
    partial = Body({'content-type':'application/json'})
    partial.append(b'{"password":"http-secret-sent')
    assert 'http-secret' not in json.dumps(partial.value())
    multipart = Body({'content-type':'multipart/form-data; boundary=x'})
    multipart.append(b'file-body-should-not-be-cached')
    assert not multipart.data


def test_server_send_failure_does_not_record_success_or_swallow_error(runtime):
    provider, exporter = runtime
    span = ins._FilteringSpan(provider.get_tracer('server').start_span('POST /retrieval/search',kind=trace.SpanKind.SERVER),
        http_attributes={'http.request.method':'POST','http.route':'/retrieval/search'},http_server=True)
    error = RuntimeError('send failed')
    async def receive():
        return {'type':'http.request','body':b'{"query":"send-input"}'}
    async def send(message):
        if message['type'] == 'http.response.body':
            span.end()  # ASGI instrumentation requests end inside its send.
            assert not exporter.get_finished_spans()
            raise error
    async def app(scope,receive,send):
        await receive()
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':b'{"query":"unsent"}'})
    with trace.use_span(span,end_on_exit=False,record_exception=False,set_status_on_exception=False):
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(ins._HttpAccessFactsMiddleware(app)({'type':'http','path':'/retrieval/search','method':'POST','headers':[]},receive,send))
    assert caught.value is error
    finished = exporter.get_finished_spans()[0]
    assert len(finished.events) == 1
    assert finished.events[0].attributes['outcome'] == 'error'
    assert finished.attributes['nkb.details.http_response.incomplete'] is True
