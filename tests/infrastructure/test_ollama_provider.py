import contextlib

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.domain.chunk import Chunk
from app.infrastructure.embedding.ollama_provider import OllamaEmbeddingProvider


def chunk(chunk_id="chunk-1", text="hello"):
    return Chunk(chunk_id, "doc-1", 0, text, "hash", 1, {"ordinal": 0})


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_embed_chunks_posts_batch_and_preserves_chunk_ids():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/embed"
        assert request.read() == b'{"model":"bge-m3","input":["hello","world"]}'
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    provider = OllamaEmbeddingProvider("http://ollama:11434", "bge-m3", client=client_for(handler))

    vectors = provider.embed_chunks([chunk("chunk-1", "hello"), chunk("chunk-2", "world")])

    assert [vector.chunk_id for vector in vectors] == ["chunk-1", "chunk-2"]
    assert [vector.model for vector in vectors] == ["bge-m3", "bge-m3"]
    assert [vector.dimensions for vector in vectors] == [2, 2]
    assert [vector.values for vector in vectors] == [[0.1, 0.2], [0.3, 0.4]]
    assert len(requests) == 1


def test_embed_query_accepts_single_embedding_shape():
    def handler(request):
        assert request.url.path == "/api/embed"
        return httpx.Response(200, json={"embedding": [1, 2, 3]})

    provider = OllamaEmbeddingProvider("http://ollama:11434", "bge-m3", client=client_for(handler))

    assert provider.embed_query("question") == [1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, text="secret source text"), httpx.Response(200, json={"bad": "shape"})],
)
def test_embedding_failure_raises_safe_runtime_error(response):
    provider = OllamaEmbeddingProvider(
        "http://ollama:11434",
        "bge-m3",
        client=client_for(lambda request: response),
    )

    with pytest.raises(RuntimeError) as exc_info:
        provider.embed_chunks([chunk(text="secret source text")])

    assert str(exc_info.value) == "embedding_provider_failed"
    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    "configured_model,tags_model",
    [("bge-m3", "bge-m3"), ("bge-m3", "bge-m3:latest"), ("bge-m3:latest", "bge-m3")],
)
def test_health_returns_ok_when_configured_model_is_present(configured_model, tags_model):
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": tags_model}]})

    provider = OllamaEmbeddingProvider(
        "http://ollama:11434",
        configured_model,
        client=client_for(handler),
    )

    assert provider.health() == {"status": "ok"}


def test_health_returns_error_when_configured_model_is_missing():
    provider = OllamaEmbeddingProvider(
        "http://ollama:11434",
        "bge-m3",
        client=client_for(lambda request: httpx.Response(200, json={"models": [{"name": "nomic-embed-text"}]})),
    )

    assert provider.health() == {"status": "error", "model": "bge-m3"}


# ---------------------------------------------------------------------------
# T7: embedding 观测回调（组合根注入的 observer scope 工厂）
# ---------------------------------------------------------------------------


class ObserverSpy:
    """记录 observer 工厂调用与 scope 生命周期的 spy。"""

    def __init__(self):
        self.calls = []

    def __call__(self, model):
        scope = _SpyScope(model)
        self.calls.append(scope)

        @contextlib.contextmanager
        def scope_cm():
            try:
                yield scope
            except BaseException as exc:
                scope.terminal = ("error", type(exc).__name__)
                raise
            else:
                scope.terminal = ("ok", None)

        return scope_cm()


class _SpyScope:
    """合同兼容的假 scope：提供 set_input_tokens(int | None)。"""

    def __init__(self, model):
        self.model = model
        self.tokens = "unset"
        self.terminal = None

    def set_input_tokens(self, tokens):
        self.tokens = tokens


def observer_provider(handler, observer):
    return OllamaEmbeddingProvider(
        "http://ollama:11434",
        "bge-m3",
        client=client_for(handler),
        embedding_observer=observer,
    )


def test_embed_chunks_enters_scope_per_request_and_reports_tokens():
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(
            200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 7}
        )

    provider = observer_provider(handler, spy)

    vectors = provider.embed_chunks([chunk(text="hello")])

    assert len(vectors) == 1
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call.model == "bge-m3"
    assert call.tokens == 7
    assert call.terminal == ("ok", None)


def test_embed_query_missing_token_count_reports_none():
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(200, json={"embedding": [1, 2, 3]})

    provider = observer_provider(handler, spy)

    assert provider.embed_query("question") == [1.0, 2.0, 3.0]
    assert spy.calls[0].tokens is None
    assert spy.calls[0].terminal == ("ok", None)


def test_embed_failure_records_terminal_state_and_reraises():
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(500, text="secret source text")

    provider = observer_provider(handler, spy)

    with pytest.raises(RuntimeError) as exc_info:
        provider.embed_chunks([chunk(text="secret source text")])

    assert str(exc_info.value) == "embedding_provider_failed"
    assert spy.calls[0].terminal == ("error", "RuntimeError")


def test_embed_query_failure_records_terminal_state():
    spy = ObserverSpy()
    provider = observer_provider(
        lambda request: httpx.Response(200, json={"bad": "shape"}), spy
    )

    with pytest.raises(RuntimeError):
        provider.embed_query("question")

    assert spy.calls[0].terminal == ("error", "RuntimeError")


def test_empty_chunks_never_enter_observer_scope():
    spy = ObserverSpy()
    provider = observer_provider(
        lambda request: pytest.fail("no request expected"), spy
    )

    assert provider.embed_chunks([]) == []
    assert spy.calls == []


def test_broken_observer_factory_is_isolated_from_business_path():
    def broken_factory(model):
        raise RuntimeError("observer exploded")

    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.5]]})

    provider = OllamaEmbeddingProvider(
        "http://ollama:11434",
        "bge-m3",
        client=client_for(handler),
        embedding_observer=broken_factory,
    )

    vectors = provider.embed_chunks([chunk(text="hello")])

    assert len(vectors) == 1
    assert vectors[0].values == [0.5]


@pytest.fixture(scope="module")
def span_exporter():
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if provider is None or type(provider).__name__ in ("ProxyTracerProvider",):
        owning = TracerProvider()
        owning.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(owning)
    else:
        try:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        except Exception:
            pass
    return exporter


def _embedding_spans(exporter):
    return [
        span for span in exporter.get_finished_spans()
        if span.name.startswith("embeddings")
    ]


def test_real_embedding_observer_emits_genai_client_span(span_exporter):
    span_exporter.clear()

    def handler(request):
        return httpx.Response(
            200, json={"embeddings": [[0.1]], "prompt_eval_count": 11}
        )

    provider = observer_provider(handler, ts.embedding_observer)

    provider.embed_chunks([chunk(text="hello")])

    spans = _embedding_spans(span_exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "embeddings bge-m3"
    assert span.attributes["gen_ai.operation.name"] == "embeddings"
    assert span.attributes["gen_ai.request.model"] == "bge-m3"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.attributes["nkb.outcome"] == "success"


def test_real_embedding_observer_records_error_terminal_state(span_exporter):
    span_exporter.clear()

    def handler(request):
        return httpx.Response(500, text="secret source text")

    provider = observer_provider(handler, ts.embedding_observer)

    with pytest.raises(RuntimeError):
        provider.embed_chunks([chunk(text="secret source text")])

    spans = _embedding_spans(span_exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error.type"] == "embedding_failed"
    assert span.attributes["nkb.outcome"] == "error"
    assert "secret" not in str(span.attributes)
