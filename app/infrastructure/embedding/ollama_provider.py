import contextlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import httpx

from app.domain.chunk import Chunk
from app.domain.embedding import EmbeddingVector

# observer scope 合同（Application 层 telemetry_semantics.embedding_observer
# 经组合根注入）：factory(model) 返回支持 __enter__/__exit__ 的 scope，
# scope 提供 set_input_tokens(int | None)。默认 None 表示无观测。
EmbeddingObserverFactory = Callable[[str], AbstractContextManager]


class OllamaEmbeddingProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        *,
        embedding_observer: EmbeddingObserverFactory | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.model = model
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._embedding_observer = embedding_observer

    @contextlib.contextmanager
    def _observe(self):
        """进入一次真实 embed 请求的观测 scope（异常隔离，不吞业务异常）。

        工厂/scope 自身故障退化为无观测；业务异常（含取消）原样传播并
        以终态结束 scope（由 scope.__exit__ 记录 outcome 与耗时）。
        """
        if self._embedding_observer is None:
            yield (lambda tokens: None, lambda inputs: None, lambda count,dimensions: None)
            return
        try:
            scope_cm = self._embedding_observer(self._model)
            scope = scope_cm.__enter__()
        except Exception:
            yield (lambda tokens: None, lambda inputs: None, lambda count,dimensions: None)
            return

        def record_tokens(tokens: Any) -> None:
            try:
                scope.set_input_tokens(tokens)
            except Exception:
                pass

        def optional(name, *args):
            try:
                callback = getattr(scope,name,None)
                if callable(callback):
                    callback(*args)
            except Exception:
                pass

        try:
            yield record_tokens, lambda inputs:optional("set_input",inputs), lambda count,dimensions:optional("set_result",count,dimensions)
        except BaseException as exc:
            try:
                scope_cm.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            raise
        try:
            scope_cm.__exit__(None, None, None)
        except Exception:
            pass

    def _post_embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        with self._observe() as (record_tokens, record_input, record_result):
            record_input(inputs)
            data = self._request_embed(inputs)
            embeddings = self._parse_embeddings(data, expected_count=len(inputs))
            # 成功解析后再填 token；缺失传 None，零 token 记 0。
            record_tokens(
                data.get("prompt_eval_count") if isinstance(data, dict) else None
            )
            record_result(len(embeddings),sorted({len(values) for values in embeddings}))
            return embeddings

    def _request_embed(self, inputs: list[str]) -> dict[str, Any]:
        try:
            response = self.client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": inputs},
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError("embedding_provider_failed")
            return response.json()
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
            # 空输入不请求、不建 span。
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
