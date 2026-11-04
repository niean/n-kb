"""Canonical completion facts have one trusted, log-only publication path."""
import io
import json
import logging
from types import MappingProxyType

from app.application import telemetry_contract as contract
from app.application.telemetry_semantics import CompletionSnapshot
from app.infrastructure.observability import logging as outlets
from app.infrastructure.observability.snapshots import project_span_facts


def test_snapshot_log_uses_exact_facts_and_rejects_spoofed_extras():
    outlets.prepare_sanitizer()
    outlets.configure_execution_context(execution_project=contract.project_execution_attributes)
    stream = io.StringIO()
    handler = outlets.ObservabilityJsonHandler(stream)
    logger = logging.getLogger(outlets.__name__)
    old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers, logger.propagate, logger.level = [handler], False, logging.INFO
    attrs = MappingProxyType({'nkb.document.operation':'upload', 'nkb.document.outcome':'success',
        'nkb.document.summary':'document.upload completed','nkb.document.duration_ms':12.5,
        'nkb.content.document_input':'safe bounded input'})
    try:
        snapshot = CompletionSnapshot('nkb.document.completed', attrs)
        outlets.emit_completion_snapshot(snapshot)
        payload = json.loads(stream.getvalue())
        assert payload['nkb.document.duration_ms'] == 12.5
        assert payload['details']['document_input']['content'] == 'safe bounded input'
        assert payload['event'] == 'nkb.document.completed'
        spoof = logging.LogRecord('app.fake', logging.INFO, '', 0, 'ordinary safe log', (), None)
        spoof.nkb_completion_snapshot = snapshot
        spoof.nkb_completion_owned = True
        spoof.http_facts = {'nkb.document.id':'spoof'}
        prepared = outlets.project_record(spoof)
        assert 'nkb.document.id' not in prepared.facts
        assert prepared.event != snapshot.event
        facts = project_span_facts(dict(attrs))
        assert facts['nkb.document.outcome'] == 'success'
        assert not any(key.startswith('nkb.content.') for key in facts)
    finally:
        logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate
        outlets._reset_for_tests()


def test_lifecycle_completion_has_identical_span_event_log_and_stderr(monkeypatch):
    import warnings
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from app.application import telemetry_details as details, telemetry_semantics as semantics
    from app.infrastructure.observability import sanitizer
    from app.infrastructure.observability.instrumentors import _FilteringTracer
    outlets._reset_for_tests()
    sanitizer.bind_details(configure=details.configure_details, project=details.project_details,
                           validate=details.validate_details, flatten=details.flatten_details)
    safe = outlets.prepare_sanitizer()
    safe.configure_secret_catalog(['a-private-secret'])
    sanitizer.configure_details(safe.sanitize, 2048)
    outlets.configure_execution_context(snapshot=semantics.current_operation_context,
        execution_project=contract.project_execution_attributes, project=details.project_details,
        validate=details.validate_details, flatten=details.flatten_details,
        details_snapshot=details.current_details, budget=details.budget_details)
    from app.composition import register_application_log_events
    register_application_log_events()
    provider = TracerProvider()
    tracer = _FilteringTracer(provider.get_tracer('completion-parity'))
    monkeypatch.setattr(semantics, '_get_tracer', lambda: tracer)
    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        bridge = outlets.SafeLogBridge(LoggingHandler(logger_provider=logs))
    output = io.StringIO()
    logger = logging.getLogger(outlets.__name__)
    old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers, logger.level, logger.propagate = [outlets.ObservabilityJsonHandler(output), bridge], logging.INFO, False
    semantics.configure_completion(outlets.emit_completion_snapshot, scope_factory=outlets.completion_scope)
    try:
        with semantics.operation_span('retrieval.search') as span:
            details.update_current_details(retrieval_input='readable a-private-secret ' + 'x'*4096)
            semantics.record_retrieval('success', result_count=0)
        actual = span._span
        assert len(actual.events) == 1
        event = dict(actual.events[0].attributes)
        sdk = exporter.get_finished_logs()[0].log_record
        logged = {key:value for key,value in sdk.attributes.items() if key != 'event' and not key.startswith('code.')}
        assert event == logged
        assert dict(actual.attributes) == event
        stderr = json.loads(output.getvalue())
        assert stderr['details']['retrieval_input']['original_bytes'] == event['nkb.content.retrieval_input_original_bytes']
        assert event['nkb.retrieval.result_count'] == 0
        assert event['nkb.content.retrieval_input_truncated'] is True
        assert 'a-private-secret' not in output.getvalue()
        assert not any(key in event for key in ('nkb.outcome','outcome','duration_ms','nkb.result_count'))
    finally:
        semantics.configure_completion()
        logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate
        logs.shutdown()
        provider.shutdown()
        outlets._reset_for_tests()
        sanitizer.configure_details(None)
        sanitizer.clear_details_bindings()


def test_plain_registered_completion_message_cannot_publish_completion_event():
    from opentelemetry.sdk.trace import TracerProvider
    from app.composition import register_application_log_events
    provider = TracerProvider()
    outlets.prepare_sanitizer()
    register_application_log_events()
    try:
        with provider.get_tracer('unowned').start_as_current_span('owner') as span:
            record = logging.LogRecord('library', logging.INFO, '', 0,
                                       'nkb.retrieval.completed', (), None)
            record.nkb_completion_owned = True
            prepared = outlets.project_record(record)
            outlets.SafeLogBridge().handle(record)
            assert not span.events
            assert prepared.event == 'library_log'
            assert prepared.message == 'nkb.retrieval.completed'
    finally:
        outlets._reset_for_tests()
        provider.shutdown()


def test_metric_projector_rejects_invalid_configured_model(monkeypatch):
    from app import composition
    from app.config import Settings
    from app.infrastructure.observability import setup
    captured = {}
    def capture(settings, **kwargs):
        captured.update(kwargs['semantics_keys'])
        return None
    monkeypatch.setattr(setup, 'setup_observability', capture)
    monkeypatch.setattr(setup, 'get_runtime_handle', lambda: None)
    for model in ('x'*65, 'invalid model name'):
        composition.start_observability(Settings(_env_file=None, otel_enabled=True,
                                                 embedding_model=model))
        projected = captured['metric_attribute_projector'](
            {'gen_ai.request.model': model}, ('gen_ai.request.model',))
        assert 'gen_ai.request.model' not in projected
