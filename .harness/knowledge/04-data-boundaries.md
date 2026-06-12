<!-- SUMMARY: N-KB 的领域数据模型、配置模型、SQLite schema、Qdrant payload、HTTP API 边界和 Docker Compose 数据挂载边界 -->
# 数据与类型边界

## 领域模型

`Document`（`app/domain/document.py`）：知识库文档聚合根，字段包括 id、title、source、content_hash、content_type、size_bytes、status、created_at、updated_at。

`DocumentSource`（`app/domain/document.py`）：文档来源值对象，字段包括 kind、uri、display_name、metadata。kind 可取 upload、local_file、web、git、api 等，当前上传来源优先落地。

`Tag`（`app/domain/tag.py`）：统一 key-value 标签值对象，字段包括 key、value。分类是固定标签键 `category=<value>`，不单独建分类树字段。

`DocumentContent`（`app/domain/document.py`）：原文内容值对象，字段包括 document_id、text、content_hash、encoding、created_at。完整原文不进入日志。

`Chunk`（`app/domain/chunk.py`）：文档切片值对象，字段包括 id、document_id、ordinal、text、content_hash、token_count、metadata。

`EmbeddingVector`（`app/domain/embedding.py`）：向量值对象，字段包括 chunk_id、model、dimensions、values。values 不写入 SQLite，不通过 HTTP API 返回。

`IndexJob`（`app/domain/indexing.py`）：入库/重建索引任务，字段包括 id、document_id、status、stage、error、created_at、updated_at。

`RetrievalQuery`（`app/domain/retrieval.py`）：检索请求领域对象，字段包括 query、filters、top_k、min_score。

`RetrievalFilter`（`app/domain/retrieval.py`）：检索过滤条件，字段包括 tags、source_kind、document_status、min_score；min_score 未传入时不设置向量库阈值，显式传入时用于过滤低相关命中。

`RetrievalResult`（`app/domain/retrieval.py`）：检索结果领域对象，字段包括 document_id、chunk_id、score、snippet、source、tags、metadata。

## 端口协议

`DocumentRepository`：定义文档、来源、原文、标签、状态和删除的读写接口。Infrastructure 的 SQLite 实现该端口。

`ChunkRepository`：定义 chunk 元数据读写接口。Infrastructure 的 SQLite 实现该端口。

`IndexJobRepository`：定义索引任务状态读写接口。Infrastructure 的 SQLite 实现该端口。

`ObjectStore`：定义原文件保存、读取和删除接口。Infrastructure 的本地文件存储实现该端口。

`DocumentParser`：定义文档解析接口。Infrastructure 的 Markdown/Text parser 和 LlamaIndex adapter 实现该端口。

`TextSplitter`：定义文本切片接口。Infrastructure 可基于 LlamaIndex 或自有实现实现该端口。

`EmbeddingProvider`：定义 embed_chunks、embed_query 接口。Infrastructure 的 Ollama embedding provider 实现该端口；chunk embedding 返回值必须保留 chunk_id 以校验向量与 chunk 对齐。

`VectorIndex`：定义 replace_document、search 接口。Infrastructure 的 Qdrant 实现该端口；replace_document 以 document_id 为边界替换该文档向量，空 chunk 列表表示清理该文档已有向量。

## 配置模型

`Settings`（`app/config.py`）：运行时配置模型，从 `.env` 和环境变量读取，前缀为 `N_KB_`。字段：

- sqlite_path
- storage_root
- qdrant_url
- qdrant_collection
- embedding_base_url
- embedding_model
- ingestion_batch_size
- max_upload_bytes

Docker Compose 项目名不属于应用配置，由 Docker Compose 读取 `COMPOSE_PROJECT_NAME`。

## SQLite schema

SQLite store 应位于 `app/infrastructure/persistence/sqlite_store.py`，初始化以下表：

```sql
documents(id, title, source_kind, source_uri, source_display_name, content_hash, content_type, size_bytes, status, created_at, updated_at)
document_contents(document_id, text, encoding, content_hash, created_at)
document_tags(document_id, tag_key, tag_value, created_at)
chunks(id, document_id, ordinal, text, content_hash, token_count, metadata_json, created_at)
index_jobs(id, document_id, status, stage, error, created_at, updated_at)
```

索引：

```sql
idx_documents_status ON documents(status)
idx_document_tags_key_value ON document_tags(tag_key, tag_value)
idx_chunks_document_ordinal ON chunks(document_id, ordinal)
idx_index_jobs_document_created_at ON index_jobs(document_id, created_at)
```

JSON 边界：

- `chunks.metadata_json` 存储 chunk 元数据
- source metadata 可在后续新增 JSON 字段或独立表
- SQLite JSON 字段在 Infrastructure 内部序列化/反序列化，不泄漏到 Domain 端口外

## Qdrant collection 边界

Qdrant collection 名称来自 `N_KB_QDRANT_COLLECTION`。向量维度以 embedding model 返回值为准，collection 初始化属于 Infrastructure 责任。

Qdrant payload 应包含：

```json
{
  "document_id": "...",
  "chunk_id": "...",
  "ordinal": 0,
  "text": "chunk text or bounded snippet source",
  "content_hash": "...",
  "document_status": "indexed",
  "tags": {"category": "...", "project": "..."},
  "source_kind": "upload",
  "source_uri": "..."
}
```

Qdrant point id 必须使用 Qdrant 支持的无符号整数或 UUID；N-KB 使用由 chunk_id 派生的稳定 UUID 作为 point id，原始 chunk_id 保留在 payload 中供 API 返回和跨存储对齐。

Qdrant payload 是 Infrastructure 存储细节，HTTP API 不直接返回原始 payload。

## HTTP API 边界

Interfaces 层请求和响应模型位于 `app/interfaces/http/`，仅作为外部协议适配：

- 上传接口接受文件、source、tags，不传入 Domain 之外的数据库字段
- 文档管理接口返回文档、来源、标签、状态和原文
- chunk 查询接口 `GET /documents/{document_id}/chunks` 返回只读 chunk 列表，用于管理页可视化入库切分结果；响应字段包括 id、document_id、ordinal、text、token_count、metadata
- 检索接口接受 query、filters、top_k、min_score，filters 可包含 tags、source_kind、document_status
- 检索响应返回结构化 `RetrievalResult` 列表，不返回底层向量、SQLite row 或 Qdrant payload

## 文件存储边界

只考虑 Docker Compose 运行时，容器内路径为：

- SQLite：`/app/locals/n-kb.db`
- 原文件与派生产物：`/app/data`

推荐存储结构：

```text
/app/data/documents/{document_id}/
  original.md       -- 上传原文件
  metadata.json     -- 文件名、content_type、hash 等非权威缓存
```

SQLite 是元数据权威来源；文件系统保存原始二进制或文本文件。

## 边界约定

- Domain 不接触 SQLite row、Qdrant SDK 对象、FastAPI UploadFile、LlamaIndex node 或 Ollama HTTP response
- Application 通过 Domain 端口访问 Parser、Embedding、VectorIndex、Repository 和 ObjectStore
- Infrastructure 负责 SDK、SQLite、Qdrant、Ollama、LlamaIndex、文件系统细节
- Interfaces 负责 HTTP 请求/响应和 FE 静态资源，不承载索引流程规则
- 原文可通过管理 API 返回给本地 FE，但日志和错误消息不得输出完整原文
- SQLite chunk 元数据更新与 Qdrant 向量替换必须保持一致；Qdrant 写入失败时恢复旧 chunk 元数据并将文档/任务标记失败
