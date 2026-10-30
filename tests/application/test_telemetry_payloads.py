"""Primary business payload and exactly-once terminal SDK contracts."""
import io
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_details as td, telemetry_semantics as ts
from app.composition import register_application_log_events
from app.infrastructure.observability import logging as obs, sanitizer
from app.application.retrieval_service import RetrievalService
from app.domain.retrieval import RetrievalFilter, RetrievalResult
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider


@pytest.fixture
def outlets(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts,'_get_tracer',lambda:provider.get_tracer('business-details'))
    obs._reset_for_tests()
    safe = obs.prepare_sanitizer()
    safe.configure_secret_catalog(['payload-secret'])
    sanitizer.bind_details(configure=td.configure_details,project=td.project_details,validate=td.validate_details,flatten=td.flatten_details)
    sanitizer.configure_details(safe.sanitize,2048)
    obs.configure_execution_context(snapshot=ts.current_operation_context,project=td.project_details,
        validate=td.validate_details,flatten=td.flatten_details,details_snapshot=td.current_details,budget=td.budget_details)
    register_application_log_events()
    output = io.StringIO()
    handler, bridge = obs.ObservabilityJsonHandler(output),obs.SafeLogBridge()
    logger = logging.getLogger('app.application')
    logger.addHandler(handler)
    logger.addHandler(bridge)
    yield exporter,output
    logger.removeHandler(handler)
    logger.removeHandler(bridge)
    provider.shutdown()
    obs._reset_for_tests()
    sanitizer.configure_details(None)


def test_retrieval_details_and_terminal_ownership(outlets):
    exporter,output = outlets
    result = RetrievalResult('doc-1','chunk-1',.8,'readable snippet payload-secret',
        {'kind':'web','uri':'https://user:pass@example.com/doc?token=private'}, {'topic':'private-tag'}, {})
    vector = SimpleNamespace(search=lambda *args:[result]*12)
    service = RetrievalService(SimpleNamespace(embed_query=lambda query:[.1,.2]),vector)
    assert len(service.search('readable query payload-secret',RetrievalFilter(tags={'topic':'private-tag'},min_score=.2),12)) == 12
    span = next(s for s in exporter.get_finished_spans() if s.name == ts.RETRIEVAL_SPAN_NAME)
    data = json.loads(span.attributes['nkb.details.retrieval_input.content'])
    result_data = json.loads(span.attributes['nkb.details.retrieval_output.content'])
    assert 'readable query' in data['query'] and data['top_k'] == 12 and data['min_score'] == .2
    assert result_data['result_count'] == 12 and result_data['returned_count'] <= 10 and result_data['results_truncated'] is True
    assert len(span.events) == 1 and span.events[0].name == 'nkb.retrieval.completed'
    assert 'payload-secret' not in output.getvalue() and 'private-tag' not in output.getvalue() and 'user:pass' not in output.getvalue()
    assert json.loads(output.getvalue().splitlines()[-1])['span_id'] == format(span.context.span_id,'016x')


def test_embedding_details_and_terminal_without_float_vectors(outlets):
    exporter,output = outlets
    client = httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'embeddings':[[.123456789,.987654321]],'prompt_eval_count':7})))
    provider = OllamaEmbeddingProvider('http://model','model',client=client,embedding_observer=ts.embedding_observer)
    assert provider.embed_query('readable embedding payload-secret') == [.123456789,.987654321]
    span = exporter.get_finished_spans()[-1]
    assert json.loads(span.attributes['nkb.details.embedding_input.content'])['input_count'] == 1
    result = json.loads(span.attributes['nkb.details.embedding_result.content'])
    assert result['output_count'] == 1 and result['dimensions'] == [2] and result['input_tokens'] == 7
    assert len(span.events) == 1
    assert 'payload-secret' not in output.getvalue() and '0.123456789' not in output.getvalue()
    client.close()


def test_upload_and_index_stage_metadata_without_document_body(outlets,tmp_path):
    from app.application.document_service import DocumentService,UploadDocumentCommand
    from app.application.ingestion_service import IngestionService
    from app.infrastructure.persistence.sqlite_store import SQLiteStore
    from app.infrastructure.storage.local_object_store import LocalObjectStore
    from app.infrastructure.parsers.text_parser import SimpleTextParser,SimpleTextSplitter
    from app.domain.embedding import EmbeddingVector
    exporter,output = outlets
    store = SQLiteStore(str(tmp_path/'documents.db'))
    vectors = SimpleNamespace(replace_document=lambda *args,**kwargs:None)
    documents = DocumentService(store,LocalObjectStore(tmp_path/'objects'),10000,vector_index=vectors)
    document = documents.upload_document(UploadDocumentCommand('notes.txt',b'file-body-never-export',None,'topic=private-tag'))
    embedding = SimpleNamespace(embed_chunks=lambda chunks:[EmbeddingVector(c.id,'model',2,[.1,.2]) for c in chunks])
    service = IngestionService(store,store,store,SimpleTextParser(),SimpleTextSplitter(),embedding,vectors)
    job = service.index_document(document.id)
    assert job.status.value == 'succeeded'
    spans = exporter.get_finished_spans()
    expected = {'nkb.document.upload','nkb.index.run','nkb.index.parse','nkb.index.split','nkb.index.embed','nkb.index.write'}
    assert {s.name for s in spans} == expected
    for span in spans:
        assert len(span.events) == 1
        assert any(key.startswith('nkb.details.') for key in span.attributes)
    assert 'file-body-never-export' not in output.getvalue() and 'private-tag' not in output.getvalue()


def test_collector_nkb_exact_details_and_no_unknown_metadata():
    from pathlib import Path
    import yaml
    path = Path(__file__).resolve().parents[3]/'n-agent/docker/observability/otel-collector.yaml'
    if not path.exists():
        pytest.skip('adjacent shared Collector not available')
    config = yaml.safe_load(path.read_text())
    statements = config['processors']['transform/loki_json']['log_statements'][0]['statements']
    nkb = [line for line in statements if 'nkb.details.' in line]
    for name in td.FIELDS:
        for key in ('content','original_bytes','truncated','incomplete','observed_bytes'):
            assert any('attributes["nkb.details.'+name+'.'+key+'"]' in line for line in nkb)
    assert all('resource.attributes["service.name"] == "n-kb"' in line for line in nkb)
    assert statements[-1] == 'set(attributes, {}) where resource.attributes["service.name"] == "n-kb"'
