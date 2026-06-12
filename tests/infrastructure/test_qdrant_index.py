from types import SimpleNamespace
import uuid

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector
from app.domain.retrieval import RetrievalFilter
from app.infrastructure.vector.qdrant_index import QdrantVectorIndex, build_point


def make_chunk(text="hello", ordinal=0):
    return Chunk(
        id=f"doc-1-chunk-{ordinal}",
        document_id="doc-1",
        ordinal=ordinal,
        text=text,
        content_hash=f"hash-{ordinal}",
        token_count=1,
        metadata={"ordinal": ordinal},
    )


def test_build_point_maps_chunk_vector_tags_and_source_to_payload():
    chunk = make_chunk("hello rag", 0)
    vector = EmbeddingVector(chunk_id=chunk.id, model="bge-m3", dimensions=2, values=[0.1, 0.2])

    point = build_point(chunk, vector, {"topic": "rag"}, {"kind": "upload", "uri": "notes.md"})

    assert point == {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "doc-1-chunk-0")),
        "vector": [0.1, 0.2],
        "payload": {
            "document_id": "doc-1",
            "chunk_id": "doc-1-chunk-0",
            "ordinal": 0,
            "text": "hello rag",
            "content_hash": "hash-0",
            "document_status": "indexed",
            "tags": {"topic": "rag"},
            "source_kind": "upload",
            "source_uri": "notes.md",
            "metadata": {"ordinal": 0},
        },
    }


class FakeQdrantClient:
    def __init__(self):
        self.collections = set()
        self.recreated = []
        self.upserts = []
        self.deletes = []
        self.searches = []
        self.search_result = []

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in self.collections])

    def recreate_collection(self, collection_name, vectors_config):
        self.collections.add(collection_name)
        self.recreated.append((collection_name, vectors_config))

    def upsert(self, collection_name, points):
        self.upserts.append((collection_name, points))

    def delete(self, collection_name, points_selector):
        self.deletes.append((collection_name, points_selector))

    def search(self, collection_name, query_vector, query_filter, limit, with_vectors):
        self.searches.append((collection_name, query_vector, query_filter, limit, with_vectors))
        return self.search_result


def test_replace_document_ensures_collection_upserts_and_deletes_stale_points():
    client = FakeQdrantClient()
    index = QdrantVectorIndex("http://qdrant:6333", "n_kb", client=client)
    chunk = make_chunk("hello", 0)
    vector = EmbeddingVector(chunk_id=chunk.id, model="bge-m3", dimensions=3, values=[0.1, 0.2, 0.3])

    index.replace_document("doc-1", [chunk], [vector], {"topic": "rag"}, {"kind": "upload", "uri": "notes.md"})

    assert client.recreated
    assert client.upserts[0][0] == "n_kb"
    assert client.upserts[0][1][0]["id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, "doc-1-chunk-0"))
    assert client.upserts[0][1][0]["payload"]["chunk_id"] == "doc-1-chunk-0"
    assert client.deletes


def test_replace_document_with_empty_chunks_deletes_document_points_without_upsert():
    client = FakeQdrantClient()
    index = QdrantVectorIndex("http://qdrant:6333", "n_kb", client=client)

    index.replace_document("doc-1", [], [], tags={}, source={})

    assert client.upserts == []
    assert len(client.deletes) == 1
    collection_name, points_selector = client.deletes[0]
    assert collection_name == "n_kb"
    point_filter = points_selector.filter
    assert point_filter.must[0].key == "document_id"
    assert point_filter.must[0].match.value == "doc-1"
    assert point_filter.must_not == []


def test_search_maps_hits_to_retrieval_results_with_bounded_snippets():
    client = FakeQdrantClient()
    long_text = "x" * 600
    client.search_result = [
        SimpleNamespace(
            id="point-1",
            score=0.87,
            payload={
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "text": long_text,
                "tags": {"topic": "rag"},
                "source_kind": "upload",
                "source_uri": "notes.md",
                "metadata": {"ordinal": 0},
            },
        )
    ]
    index = QdrantVectorIndex("http://qdrant:6333", "n_kb", client=client)

    results = index.search([0.1, 0.2], RetrievalFilter(tags={"topic": "rag"}, source_kind="upload"), top_k=1)

    assert len(results) == 1
    result = results[0]
    assert result.document_id == "doc-1"
    assert result.chunk_id == "chunk-1"
    assert result.score == 0.87
    assert len(result.snippet) <= 500
    assert result.source == {"kind": "upload", "uri": "notes.md"}
    assert result.tags == {"topic": "rag"}
    assert result.metadata == {"ordinal": 0}
    assert client.searches[0][:4] == ("n_kb", [0.1, 0.2], index._build_filter(RetrievalFilter(tags={"topic": "rag"}, source_kind="upload")), 1)
    assert client.searches[0][4] is False


def test_health_returns_ok_when_collections_can_be_fetched():
    client = FakeQdrantClient()
    index = QdrantVectorIndex("http://qdrant:6333", "n_kb", client=client)

    assert index.health() == {"status": "ok"}
