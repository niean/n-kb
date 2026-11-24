"""语料修订（corpus revision）计算与缓存。

检索开始时读取一次 corpus_revision 作为 RAG 版本化证据：revision 由 indexed
文档清单（id -> content_hash）与分块/嵌入配置共同决定，任何清单成员或配置
变化都会翻转 revision。重算失败返回 None，检索继续，不阻断业务；CAS 写入
失败（并发写已递增 generation）同样返回 None 并保留 dirty，等待下次重算。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.domain.evaluation import canonical_json_bytes
from app.domain.ports import CorpusStateStore

# 清单结构版本；documents/chunking/embedding 字段语义变化时递增
CORPUS_REVISION_SCHEMA = 1


def fingerprint_from_splitter(splitter: Any) -> str:
    """从 splitter 实例推导配置指纹：类型名 + 分块参数。

    供组合根在装配时由实际 splitter 实例派生 chunking_fingerprint；
    本函数为纯函数，不依赖服务实例。
    """
    # 仅取裸类名：组合根只有单一 splitter，不同模块同名 splitter 会碰撞；
    # 实例缺 chunk_size/chunk_overlap 属性时在装配期立即失败（fail-fast）。
    return f"{type(splitter).__name__}:{splitter.chunk_size}:{splitter.chunk_overlap}"


@dataclass(frozen=True)
class CorpusRevisionConfig:
    chunking_fingerprint: str
    embedding_provider: str
    embedding_model: str
    index_version: int = 1


class CorpusRevisionService:
    def __init__(self, store: CorpusStateStore, config: CorpusRevisionConfig) -> None:
        self._store = store
        self._config = config
        self._config_fingerprint = self._compute_config_fingerprint()

    def current(self) -> str | None:
        """返回当前语料修订；缓存命中直接返回，否则重算并 CAS 发布。

        重算口径：canonical JSON {"schema", "documents"(仅 indexed、按 id 序),
        "chunking", "embedding", "index_version"} -> sha256 hex。任何异常
        （store 读写、序列化等内部计算错误）都不上抛，返回 None 交给检索方
        按未版本化处理。
        """
        try:
            state = self._store.read_corpus_state()
            if (
                state is not None
                and not state["dirty"]
                and state["config_fingerprint"] == self._config_fingerprint
            ):
                value = state["value"]
                if isinstance(value, dict) and "revision" in value:
                    return value["revision"]
                # 缓存值损坏（缺 revision 键）：视为缓存未命中，走重算自愈
            documents, generation = self._store.read_corpus_snapshot()
            revision = hashlib.sha256(canonical_json_bytes(self._payload(documents))).hexdigest()
            return self._publish(revision=revision, generation=generation)
        except Exception:
            return None

    def mark_dirty(self) -> None:
        """置脏入口：委托 store 递增 generation 并置 dirty；store 错误原样上抛，不做吞噬。"""
        self._store.mark_corpus_dirty()

    def _payload(self, documents: dict[str, str]) -> dict:
        return {
            "schema": CORPUS_REVISION_SCHEMA,
            "documents": documents,
            "chunking": {"fingerprint": self._config.chunking_fingerprint},
            "embedding": {
                "provider": self._config.embedding_provider,
                "model": self._config.embedding_model,
            },
            "index_version": self._config.index_version,
        }

    def _compute_config_fingerprint(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._payload({}))).hexdigest()

    def _publish(self, revision: str, generation: int) -> str | None:
        published = self._store.write_corpus_state(
            {"revision": revision},
            expected_generation=generation,
            config_fingerprint=self._config_fingerprint,
        )
        return revision if published else None
