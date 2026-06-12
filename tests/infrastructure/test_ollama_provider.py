import httpx
import pytest

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
