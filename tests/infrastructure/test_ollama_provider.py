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


# ---------------------------------------------------------------------------
# T6: 计量承接次序（HTTP 成功后先记 token 再解析向量）与缺失/非法区分
# ---------------------------------------------------------------------------


def test_tokens_recorded_before_vector_parse_survives_parse_failure():
    # 解析失败（形状错误）时已知 Token 已承接：保留计量，业务异常照常传播
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(200, json={"prompt_eval_count": 7})

    provider = observer_provider(handler, spy)

    with pytest.raises(RuntimeError):
        provider.embed_chunks([chunk(text="hello")])

    assert spy.calls[0].tokens == 7
    assert spy.calls[0].terminal == ("error", "RuntimeError")


def test_missing_metering_after_http_success_records_none():
    # HTTP 成功但缺计量字段：记 None（missing），不猜测、不回退
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(200, json={"prompt_eval_count": None})

    provider = observer_provider(handler, spy)

    with pytest.raises(RuntimeError):
        provider.embed_chunks([chunk(text="hello")])

    assert spy.calls[0].tokens is None
    assert spy.calls[0].terminal == ("error", "RuntimeError")


@pytest.mark.parametrize("raw", [-3, "7", True, 1.5])
def test_invalid_metering_is_passed_through_untouched(raw):
    # 存在但非法的计量值原样透传，由观测侧区分 invalid（不折叠成 missing）
    spy = ObserverSpy()

    def handler(request):
        return httpx.Response(
            200, json={"embeddings": [[0.1]], "prompt_eval_count": raw}
        )

    provider = observer_provider(handler, spy)

    provider.embed_chunks([chunk(text="hello")])

    assert spy.calls[0].tokens == raw
    assert spy.calls[0].terminal == ("ok", None)


def test_http_failure_never_reports_metering():
    # HTTP 失败：不产生计量事实（scope 未承接），业务异常传播
    spy = ObserverSpy()
    provider = observer_provider(
        lambda request: httpx.Response(500, text="secret source text"), spy
    )

    with pytest.raises(RuntimeError):
        provider.embed_query("question")

    assert spy.calls[0].tokens == "unset"
    assert spy.calls[0].terminal == ("error", "RuntimeError")


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
    assert span.attributes["nkb.model.outcome"] == "success"


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
    assert span.attributes["nkb.model.outcome"] == "failure"
    assert "secret" not in str(span.attributes)


# ---------------------------------------------------------------------------
# T6: 组合根计价闭包端到端（真实 observer + 目录 + 受信身份）
# ---------------------------------------------------------------------------


def _configure_cost_calculator():
    """用仓库自带目录与受信身份构造组合根计价闭包。"""
    from pathlib import Path

    from app.composition import make_embedding_cost_calculator
    from app.config import Settings

    settings = Settings(
        pricing_catalog_path=str(
            Path(__file__).resolve().parents[2]
            / "docker"
            / "observability"
            / "model-pricing.json"
        ),
        pricing_catalog_expected_sha256=(
            "3f31783a1b457ce213b8482a23243dbfd00e16e8ed7ff9086ca600d2be1ec19f"
        ),
    )
    ts.configure_embedding_cost_calculator(
        make_embedding_cost_calculator(settings)
    )


def test_real_observer_with_catalog_reports_explicit_local_zero_cost(span_exporter):
    span_exporter.clear()
    _configure_cost_calculator()
    try:

        def handler(request):
            return httpx.Response(
                200, json={"embeddings": [[0.1]], "prompt_eval_count": 11}
            )

        provider = observer_provider(handler, ts.embedding_observer)
        provider.embed_chunks([chunk(text="hello")])
    finally:
        ts.configure_embedding_cost_calculator()

    spans = _embedding_spans(span_exporter)
    assert len(spans) == 1
    span = spans[0]
    # 本地 bge-m3 显式零价，合法用量仍须经过真实计价闭包。
    assert span.attributes["nkb.model.cost_status"] == "estimated"
    assert "nkb.model.cost_reason" not in span.attributes
    assert span.attributes["nkb.model.billing_provider"] == "ollama"
    assert span.attributes["nkb.model.billing_model"] == "bge-m3"
    assert span.attributes["nkb.model.pricing_version"].startswith("2026-11-10-peak-fx7-v1@")
    assert span.attributes["nkb.model.cost_usd"] == 0
    assert span.attributes["nkb.model.cost_decimal"] == "0"
    assert span.attributes["nkb.model.cost_input_usd"] == 0


def test_real_observer_without_calculator_has_no_cost_facts(span_exporter):
    span_exporter.clear()

    def handler(request):
        return httpx.Response(
            200, json={"embeddings": [[0.1]], "prompt_eval_count": 11}
        )

    provider = observer_provider(handler, ts.embedding_observer)
    provider.embed_chunks([chunk(text="hello")])

    span = _embedding_spans(span_exporter)[0]
    assert "nkb.model.cost_status" not in span.attributes
    assert "nkb.model.cost_reason" not in span.attributes
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
