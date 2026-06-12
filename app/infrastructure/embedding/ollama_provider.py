from typing import Any

import httpx

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector


class OllamaEmbeddingProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.model = model
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def _post_embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = self.client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": inputs},
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError("embedding_provider_failed")
            data = response.json()
            embeddings = self._parse_embeddings(data, expected_count=len(inputs))
            return embeddings
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("embedding_provider_failed") from exc

    @staticmethod
    def _parse_embeddings(data: dict[str, Any], expected_count: int) -> list[list[float]]:
        if isinstance(data.get("embeddings"), list):
            embeddings = data["embeddings"]
        elif isinstance(data.get("embedding"), list):
            embeddings = [data["embedding"]]
        else:
            raise RuntimeError("embedding_provider_failed")
        if len(embeddings) != expected_count:
            raise RuntimeError("embedding_provider_failed")
        parsed: list[list[float]] = []
        for embedding in embeddings:
            if not isinstance(embedding, list):
                raise RuntimeError("embedding_provider_failed")
            try:
                parsed.append([float(value) for value in embedding])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("embedding_provider_failed") from exc
        return parsed

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddingVector]:
        if not chunks:
            return []
        embeddings = self._post_embed([chunk.text for chunk in chunks])
        return [
            EmbeddingVector(
                chunk_id=chunk.id,
                model=self.model,
                dimensions=len(values),
                values=values,
            )
            for chunk, values in zip(chunks, embeddings, strict=True)
        ]

    def embed_query(self, query: str) -> list[float]:
        return self._post_embed([query])[0]

    def health(self) -> dict[str, object]:
        try:
            response = self.client.get(f"{self.base_url}/api/tags")
            if response.status_code < 200 or response.status_code >= 300:
                return {"status": "error", "model": self._model}
            data = response.json()
            if self._model_is_present(data):
                return {"status": "ok"}
        except Exception:
            pass
        return {"status": "error", "model": self._model}

    def _model_is_present(self, data: dict[str, Any]) -> bool:
        configured_names = self._model_aliases(self._model)
        for model in data.get("models", []):
            if not isinstance(model, dict):
                continue
            candidate = model.get("name") or model.get("model")
            if isinstance(candidate, str) and configured_names.intersection(self._model_aliases(candidate)):
                return True
        return False

    @staticmethod
    def _model_aliases(model: str) -> set[str]:
        aliases = {model}
        if model.endswith(":latest"):
            aliases.add(model.removesuffix(":latest"))
        elif ":" not in model:
            aliases.add(f"{model}:latest")
        return aliases
