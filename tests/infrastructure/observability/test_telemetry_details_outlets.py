"""Real SDK contracts for prepared details, readable logs and owner identity."""
import io
import json
import logging
import warnings

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler, LogRecordLimits
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from app.application import telemetry_details as details
from app.infrastructure.observability import logging as obs
from app.infrastructure.observability import sanitizer


@pytest.fixture(autouse=True)
def policy():
    obs._reset_for_tests()
    sanitizer.bind_details(configure=details.configure_details, project=details.project_details,
                           validate=details.validate_details, flatten=details.flatten_details)
    safe = obs.prepare_sanitizer()
    safe.configure_secret_catalog(['secret-outlet-sentinel'])
    sanitizer.configure_details(safe.sanitize, 2048)
    obs.configure_execution_context(project=details.project_details, validate=details.validate_details,
        flatten=details.flatten_details, details_snapshot=details.current_details, budget=details.budget_details)
    yield
    obs._reset_for_tests()
    sanitizer.configure_details(None)


def record(message='readable %s', args=('answer',), level=logging.INFO, name='app.test'):
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


def test_unknown_logs_readable_and_extras_fresh_projected():
    raw = record('query %s secret-outlet-sentinel', ('readable',))
    raw.payload = {'query': 'hi', 'tags': ['private'], 'vector': [1., 2.], 'file_content':'file-secret'}
    raw.details = {'retrieval_input': {'content':json.dumps({'file_content':'file-secret'}), 'original_bytes':1,'truncated':False}}
    raw.trace_id = 'spoof'
    raw._prepared = True
    prepared = obs.project_record(raw)
    assert prepared.event == 'application_log'
    assert 'query readable' in prepared.message
    assert 'secret-outlet-sentinel' not in prepared.message
    text = json.dumps(prepared.details)
    assert 'hi' in text and 'private' not in text and 'file-secret' not in text
    assert set(prepared.details) == {'log_fields'}
    assert obs.project_record(record(name='library')).event == 'library_log'
    assert obs.project_record(record(level=logging.WARNING)).event == 'diagnostic_log'


def test_hostile_formatters_and_exception_chain_never_execute():
    class Hostile:
        def __str__(self):
            raise AssertionError('called str')
        def __repr__(self):
            raise AssertionError('called repr')
    class HostileError(Exception):
        def __str__(self):
            raise AssertionError('called exception str')
    raw = record('value %s', (Hostile(),))
    error = HostileError('readable error secret-outlet-sentinel')
    raw.exc_info = (HostileError, error, None)
    prepared = obs.project_record(raw)
    assert 'Hostile' in prepared.message
    assert 'readable error' in prepared.exception['message']
    assert 'secret-outlet-sentinel' not in json.dumps(prepared.exception)


def test_same_prepared_details_reach_stderr_event_and_sdk():
    span_provider = TracerProvider()
    exporter = InMemoryLogRecordExporter()
    log_provider = LoggerProvider(log_record_limits=LogRecordLimits(max_attributes=256, max_attribute_length=65536))
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        bridge = obs.SafeLogBridge(LoggingHandler(logger_provider=log_provider))
    raw = record('done %s', ('answer',))
    obs.register_safe_event('done %s', 'execution_completed')
    output = io.StringIO()
    handler = obs.ObservabilityJsonHandler(output)
    with span_provider.get_tracer('outlets').start_as_current_span('operation') as span:
        with details.detail_scope(retrieval_input={'query':'readable secret-outlet-sentinel'}):
            handler.handle(raw)
            bridge.handle(raw)
    payload = json.loads(output.getvalue())
    event = span.events[0]
    sdk = exporter.get_finished_logs()[0].log_record
    assert event.name == 'execution_completed'
    assert payload['details']['retrieval_input']['content'] == event.attributes['nkb.details.retrieval_input.content']
    assert sdk.attributes['nkb.details.retrieval_input.content'] == event.attributes['nkb.details.retrieval_input.content']
    assert sdk.span_id == span.context.span_id
    assert payload['span_id'] == format(sdk.span_id,'016x')
    assert 'secret-outlet-sentinel' not in output.getvalue()
    log_provider.shutdown()
    span_provider.shutdown()


@pytest.mark.parametrize('limit',[1,2048,65536])
def test_actual_record_budget_and_item_limit(limit):
    sanitizer.configure_details(obs.ensure_sanitizer().sanitize, limit)
    obs.configure_execution_context(project=details.project_details, validate=details.validate_details,
        flatten=details.flatten_details, details_snapshot=details.current_details, budget=details.budget_details,
        max_bytes=limit)
    raw = record('x' * (limit * 5), ())
    raw.payload = {'query':'汉' * limit}
    with details.detail_scope(error='E'*limit,retrieval_input='I'*limit,retrieval_output='O'*limit):
        prepared = obs.project_record(raw)
    actual = json.dumps({'message':prepared.message,'exception':prepared.exception,'details':prepared.details},ensure_ascii=False)
    assert len(actual.encode()) <= limit*4 or limit == 1 and not prepared.details and prepared.message is None
    flat = {key:value for key,value in prepared.attributes.items() if key not in prepared.facts}
    actual_flat = json.dumps({'body':prepared.message,'attributes':flat},ensure_ascii=False)
    assert len(actual_flat.encode()) <= limit*4 or limit == 1 and not flat
    for envelope in prepared.details.values():
        assert len(envelope['content'].encode()) <= limit


def test_long_plain_content_sanitizer_has_scheme_boundary():
    assert obs.ensure_sanitizer().sanitize('a' * 65536) == 'a' * 65536
    assert obs.ensure_sanitizer().sanitize('https://user:pass@example.com') == 'https://[REDACTED]@example.com'


def test_filtering_span_only_exact_fresh_detail_envelopes_and_safe_events():
    from app.infrastructure.observability.instrumentors import _FilteringSpan
    provider = TracerProvider()
    span = provider.get_tracer('filter').start_span('filtered')
    proxy = _FilteringSpan(span)
    safe = details.project_details(retrieval_input={'query':'readable'})
    proxy.set_attributes(details.flatten_details(safe))
    proxy.set_attribute('nkb.details.retrieval_input.other','forbidden')
    proxy.set_attribute('nkb.details.unknown.content','forbidden')
    proxy.set_attributes({'nkb.details.document_input.content':json.dumps({'file_content':'forbidden-file'}),
        'nkb.details.document_input.original_bytes':100, 'nkb.details.document_input.truncated':False})
    obs.register_safe_event('done %s','execution_completed')
    proxy.add_event('execution_completed',dict(details.flatten_details(safe), arbitrary='forbidden'))
    proxy.record_exception(ValueError('forbidden-exception'))
    proxy.set_status(trace.Status(trace.StatusCode.ERROR,'forbidden-description'))
    proxy.end()
    assert span.attributes['nkb.details.retrieval_input.content'] == safe['retrieval_input']['content']
    assert 'forbidden' not in json.dumps(dict(span.attributes))
    assert span.events[0].name == 'execution_completed'
    assert 'arbitrary' not in span.events[0].attributes
    assert span.status.description is None
    provider.shutdown()


def test_explicit_owner_terminal_event_and_unsampled_log():
    assert hasattr(obs,'emit_terminal_event')
    provider = TracerProvider()
    owner = provider.get_tracer('owner').start_span('owner')
    other = provider.get_tracer('owner').start_span('other')
    output = io.StringIO()
    handler = obs.ObservabilityJsonHandler(output)
    bridge = obs.SafeLogBridge()
    logger = logging.getLogger(obs.__name__)
    old = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(bridge)
    try:
        obs.register_safe_event('finished %s','execution_completed')
        with trace.use_span(other,end_on_exit=False):
            obs.emit_terminal_event('execution_completed',facts={'outcome':'success'},
                details=details.project_details(retrieval_input='owner-input'),owner_context=owner)
        assert json.loads(output.getvalue())['span_id'] == format(owner.context.span_id,'016x')
        assert len(owner.events) == 1 and not other.events
        assert owner.attributes['nkb.details.retrieval_input.content'] == 'owner-input'
        assert owner.events[0].attributes['nkb.details.retrieval_input.content'] == 'owner-input'
        unsampled = trace.NonRecordingSpan(trace.SpanContext(123,456,False,trace.TraceFlags(0)))
        with trace.use_span(other,end_on_exit=False):
            obs.log_http_request_completed('POST','/retrieval/search',200,1,'completed',
                details=details.project_details(http_request='body'),owner_context=unsampled)
        assert json.loads(output.getvalue().splitlines()[-1])['span_id'] == format(456,'016x')
    finally:
        logger.removeHandler(handler)
        logger.removeHandler(bridge)
        logger.setLevel(old)
        owner.end()
        other.end()
        provider.shutdown()


def test_real_sdk_limits_support_65536_content_without_cutting_metadata():
    from app.infrastructure.observability.setup import _detail_sdk_limits
    span_limits, log_limits = _detail_sdk_limits(65536)
    provider = TracerProvider(span_limits=span_limits)
    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider(log_record_limits=log_limits)
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    sanitizer.configure_details(obs.ensure_sanitizer().sanitize,65536)
    obs.configure_execution_context(project=details.project_details,validate=details.validate_details,
        flatten=details.flatten_details,details_snapshot=details.current_details,budget=details.budget_details,max_bytes=65536)
    obs.register_safe_event('large done','execution_completed')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',DeprecationWarning)
        bridge = obs.SafeLogBridge(LoggingHandler(logger_provider=logs))
    with provider.get_tracer('large').start_as_current_span('large') as span:
        with details.detail_scope(retrieval_input='汉'*21845 + 'a'):
            raw = record('large done',())
            bridge.handle(raw)
    exported = exporter.get_finished_logs()[0].log_record
    key = 'nkb.details.retrieval_input.content'
    assert len(span.attributes[key].encode()) == 65536
    assert span.attributes[key] == span.events[0].attributes[key]
    assert len(span.events[0].attributes[key].encode()) == 65536
    assert exported.attributes[key] == span.events[0].attributes[key]
    assert exported.attributes['nkb.details.retrieval_input.original_bytes'] == 65536
    assert exported.attributes['nkb.details.retrieval_input.truncated'] is False
    provider.shutdown()
    logs.shutdown()


def test_formatting_has_global_budget_partial_secret_and_dynamic_width_fail_closed():
    limit = 2048
    secret = 'UNIQUE' + 'x' * 10000
    obs.ensure_sanitizer().configure_secret_catalog([secret])
    raw = record('%s', (secret,))
    assert 'UNIQUE' not in (obs.project_record(raw).message or '')
    assert obs.project_record(record('%*s',(100000,'x'))).message == '[log format unavailable]'
    shared = ['x'] * 32
    for _ in range(8):
        shared = [shared] * 32
    assert len(obs.project_record(record('tree %s',(shared,))).message.encode()) <= limit


def test_forged_detail_extras_preserve_safe_information_without_policy_bypass():
    raw = record()
    raw.details = {'retrieval_input':{'content':json.dumps({'query':'keep query','file_content':'drop-file'}),
        'original_bytes':1,'truncated':False}}
    raw.__dict__['nkb.details.log_fields.content'] = json.dumps({'query':'keep flat','tags':['drop-tag']})
    raw._prepared = object()
    prepared = obs.project_record(raw)
    text = json.dumps(prepared.details)
    assert 'keep query' in text and 'keep flat' in text
    assert 'drop-file' not in text and 'drop-tag' not in text
    assert set(prepared.details) == {'log_fields'}


def test_filtered_owner_preserves_one_prepared_truncation_metadata():
    from app.infrastructure.observability.instrumentors import _FilteringSpan
    provider = TracerProvider()
    raw_span = provider.get_tracer('prepared').start_span('owner')
    owner = _FilteringSpan(raw_span)
    obs.register_safe_event('completed','execution_completed')
    raw = record('completed',())
    output = io.StringIO()
    handler = obs.ObservabilityJsonHandler(output)
    with trace.use_span(owner,end_on_exit=False):
        with details.detail_scope(retrieval_input='x'*4096):
            handler.handle(raw)
            obs.SafeLogBridge().handle(raw)
    payload = json.loads(output.getvalue())
    prefix = 'nkb.details.retrieval_input.'
    assert raw_span.events[0].attributes[prefix+'original_bytes'] == payload['details']['retrieval_input']['original_bytes'] == 4096
    assert raw_span.events[0].attributes[prefix+'truncated'] is True
    owner.end()
    assert raw_span.attributes[prefix+'original_bytes'] == 4096
    assert raw_span.attributes[prefix+'content'] == raw_span.events[0].attributes[prefix+'content']
    provider.shutdown()


def test_projection_callback_failure_keeps_other_readable_fields_and_diagnostic():
    def fail(**fields):
        raise RuntimeError('secret-outlet-sentinel')
    obs.configure_execution_context(project=fail,validate=details.validate_details,
        flatten=details.flatten_details,details_snapshot=details.current_details,budget=details.budget_details)
    raw = record()
    raw.payload = {'query':'ignored'}
    with details.detail_scope(retrieval_input='safe input'):
        prepared = obs.project_record(raw)
    assert prepared.message == 'readable answer'
    assert prepared.details['retrieval_input']['content'] == 'safe input'
    assert prepared.facts['details_unavailable'] is True


def test_bridge_recursion_isolated_and_raw_failure_never_reaches_stderr(capsys):
    count = []
    class Recursive(logging.Handler):
        def emit(self, raw):
            count.append(raw)
            bridge.handle(record('secret-outlet-sentinel',()))
            raise RuntimeError('secret-outlet-sentinel')
    bridge = obs.SafeLogBridge(Recursive())
    bridge.handle(record())
    assert len(count) == 1 and bridge.failure_count == 1
    class Broken:
        def write(self, text):
            raise ValueError('secret-outlet-sentinel')
    obs.ObservabilityJsonHandler(Broken()).handle(record())
    assert 'secret-outlet-sentinel' not in capsys.readouterr().err


def test_secret_policy_changed_mid_projection_fails_closed_for_entire_record():
    def project(**fields):
        obs.ensure_sanitizer().configure_secret_catalog(['new-secret'])
        return details.project_details(**fields)
    obs.configure_execution_context(project=project,validate=details.validate_details,
        flatten=details.flatten_details,details_snapshot=details.current_details,budget=details.budget_details)
    raw = record('new-secret',())
    raw.payload = {'query':'hello'}
    prepared = obs.project_record(raw)
    assert 'new-secret' not in json.dumps(dict(message=prepared.message,details=prepared.details))
    assert prepared.facts['details_unavailable'] is True


def test_log_identity_text_cannot_leak_catalog_secret():
    raw = record(name='app.secret-outlet-sentinel')
    payload = obs.SafeEventJsonFormatter().format(raw)
    assert 'secret-outlet-sentinel' not in payload


def test_real_sdk_exception_chain_matches_stderr_and_contains_no_locals():
    provider = TracerProvider()
    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',DeprecationWarning)
        bridge = obs.SafeLogBridge(LoggingHandler(logger_provider=logs))
    obs.register_safe_event('failed operation','execution_completed')
    raw = record('failed operation',(),logging.ERROR)
    local_private_value = 'never-render-this-local'
    try:
        try:
            raise ValueError('cause readable secret-outlet-sentinel')
        except ValueError as cause:
            raise RuntimeError('outer readable secret-outlet-sentinel') from cause
    except RuntimeError:
        import sys
        raw.exc_info = sys.exc_info()
    with provider.get_tracer('exception').start_as_current_span('operation') as span:
        payload = json.loads(obs.SafeEventJsonFormatter().format(raw))
        bridge.handle(raw)
    sdk = exporter.get_finished_logs()[0].log_record
    assert 'cause readable' in payload['exception']['stacktrace']
    assert 'outer readable' in payload['exception']['message']
    for key,value in payload['exception'].items():
        assert sdk.attributes['exception.'+key] == value
        assert span.events[0].attributes['exception.'+key] == value
    text = json.dumps(payload)
    assert local_private_value not in text and 'secret-outlet-sentinel' not in text
    provider.shutdown()
    logs.shutdown()


def test_json_handler_callback_logging_and_failure_do_not_recurse(capsys):
    logger = logging.getLogger('app.projection-recursion-test')
    output = io.StringIO()
    handler = obs.ObservabilityJsonHandler(output)
    old_level, old_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    calls = []
    def project(**fields):
        calls.append(True)
        logger.info('secret-outlet-sentinel', extra={'payload':'nested'})
        raise RuntimeError('secret-outlet-sentinel')
    obs.configure_execution_context(project=project, validate=details.validate_details,
        flatten=details.flatten_details,details_snapshot=details.current_details,budget=details.budget_details)
    try:
        logger.info('outer readable',extra={'payload':'query'})
        assert len(calls) == 1
        payloads = output.getvalue().splitlines()
        assert len(payloads) == 1
        assert json.loads(payloads[0])['details_unavailable'] is True
        assert 'secret-outlet-sentinel' not in output.getvalue() + capsys.readouterr().err
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_raw_registered_span_event_has_same_total_budget_and_safe_partial_text():
    from app.infrastructure.observability.instrumentors import _FilteringSpan
    provider = TracerProvider()
    span = provider.get_tracer('raw-event').start_span('owner')
    owner = _FilteringSpan(span)
    obs.register_safe_event('raw event','execution_completed')
    values = {name:'汉'*2048 for name in details.FIELDS}
    attributes = details.flatten_details(details.project_details(**values))
    attributes.update({'message':'M'*2048,'exception.type':'ValueError','exception.message':'E'*2048,
                       'exception.stacktrace':'S'*2048})
    owner.add_event('execution_completed',attributes)
    assert 'nkb.details.error.content' in span.events[0].attributes
    assert len(json.dumps(dict(span.events[0].attributes),ensure_ascii=False).encode()) <= 8192
    secret = 'UNIQUE' + 'x'*10000
    obs.ensure_sanitizer().configure_secret_catalog([secret])
    owner.add_event('execution_completed',{'message':secret})
    assert 'UNIQUE' not in json.dumps(dict(span.events[-1].attributes))
    owner.end()
    provider.shutdown()


@pytest.mark.parametrize('message',[None,'',{},[]])
def test_empty_placeholders_are_filtered(message):
    assert obs.project_record(record(message,())).dropped is True


def test_format_containers_use_central_policy_and_preformatted_credentials_are_redacted():
    for raw in (record('value %s',({'password':'veryprivate','query':'readable','count':3},)),
                record("payload={'password': 'veryprivate'}",()),
                record('nested %s',([{'token':"escaped\\'secret",'file_content':'private-file'}],))):
        prepared = obs.project_record(raw)
        assert 'veryprivate' not in (prepared.message or '')
        assert 'private-file' not in (prepared.message or '')
        assert "escaped" not in (prepared.message or '')
    raw = record('count %(count)d query %(query)s',{'count':3,'query':'readable','password':'veryprivate'})
    assert obs.project_record(raw).message == 'count 3 query readable'
    obs.configure_execution_context()
    assert 'veryprivate' not in (obs.project_record(record('value %s',({'password':'veryprivate'},))).message or '')


def test_catalog_secret_in_exception_class_name_never_leaks():
    obs.ensure_sanitizer().configure_secret_catalog(['SentinelSecret'])
    error_type = type('SentinelSecret',(Exception,),{})
    raw = record('failed',(),logging.ERROR)
    raw.exc_info = (error_type,error_type('readable'),None)
    prepared = obs.project_record(raw)
    assert prepared.exception_type == 'Exception'
    assert 'SentinelSecret' not in obs.SafeEventJsonFormatter().format(raw)


@pytest.mark.parametrize('limit',[1,2048,65536])
@pytest.mark.parametrize('bulk',[False,True,'initial'])
def test_direct_span_detail_writes_share_actual_flat_budget(limit,bulk):
    from app.infrastructure.observability.instrumentors import _FilteringSpan, _FilteringTracer
    from app.infrastructure.observability.setup import _detail_sdk_limits
    sanitizer.configure_details(obs.ensure_sanitizer().sanitize,limit)
    obs.configure_execution_context(project=details.project_details,validate=details.validate_details,
        flatten=details.flatten_details,details_snapshot=details.current_details,budget=details.budget_details,max_bytes=limit)
    limits,_ = _detail_sdk_limits(limit)
    provider = TracerProvider(span_limits=limits)
    span = provider.get_tracer('direct-budget').start_span('owner')
    owner = _FilteringSpan(span)
    attrs = {'nkb.details.'+name+'.content':'x'*limit for name in details.FIELDS}
    if bulk == 'initial':
        span.end()
        owner = _FilteringTracer(provider.get_tracer('direct-budget')).start_span('owner',attributes=attrs)
        span = owner._span
    elif bulk:
        owner.set_attributes(attrs)
    else:
        for key,value in attrs.items():
            owner.set_attribute(key,value)
    owner.end()
    flat = {key:value for key,value in span.attributes.items() if key.startswith('nkb.details.')}
    assert len(json.dumps(flat,ensure_ascii=False).encode()) <= limit*4
    if limit > 1:
        assert flat and any(key.endswith('.truncated') and value for key,value in flat.items())
    provider.shutdown()


@pytest.mark.parametrize('text',[
    "payload={'password': 'veryprivate\\'escaped'} readable",
    "payload={'password': 'veryprivate\\",
    'payload={"password": "veryprivate\\"escaped"} readable',
    "payload={'password': 'veryprivate",
    "payload={'password': 'prefix\\\nveryprivate'}",
])
def test_preformatted_credential_escaped_and_incomplete_tails(text):
    assert 'veryprivate' not in (obs.project_record(record(text,())).message or '')


@pytest.mark.parametrize('text',["payload={'password': 123456789}",'payload={"token": 123456789}'])
def test_preformatted_credential_numeric_values_follow_same_policy(text):
    assert '123456789' not in (obs.project_record(record(text,())).message or '')


def test_buffered_span_details_revalidate_after_catalog_changes_and_fact_update():
    from app.infrastructure.observability.instrumentors import _FilteringSpan
    provider = TracerProvider()
    span = provider.get_tracer('policy').start_span('owner')
    owner = _FilteringSpan(span)
    owner.set_attribute('nkb.details.retrieval_input.content','new-catalog-secret')
    obs.ensure_sanitizer().configure_secret_catalog(['new-catalog-secret'])
    owner.set_attributes({'http.request.method':'POST'})
    owner.end()
    assert 'new-catalog-secret' not in json.dumps(dict(span.attributes))
    provider.shutdown()
