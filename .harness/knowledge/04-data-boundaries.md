<!-- SUMMARY: N-KB 的领域数据模型、配置模型、SQLite schema、Qdrant payload、HTTP API 边界和 Docker Compose 数据挂载边界；AttemptCost 域数据结构与 nkb.model.* 成本属性键；定价目录配置（N_KB_PRICING_CATALOG_PATH/EXPECTED_SHA256，B 类 env-only，JCS 哈希锁定，与 N-Agent 部署相同内容目录） -->
# 数据与类型边界

## 领域模型

`Document`（`app/domain/document.py`）：知识库文档聚合根，字段包括 id、title、source、content_hash、content_type、size_bytes、status、created_at、updated_at。

`DocumentSource`（`app/domain/document.py`）：文档来源值对象，字段包括 kind、uri、display_name、metadata。kind 可取 upload、local_file、web、git、api 等，当前上传来源优先落地。

`Tag`（`app/domain/tag.py`）：统一 key-value 标签值对象，字段包括 key、value。分类是固定标签键 `category=<value>`，不单独建分类树字段。

`DocumentContent`（`app/domain/document.py`）：原文内容值对象，字段包括 document_id、text、content_hash、encoding、created_at。完整原文不进入日志。

`Chunk`（`app/domain/chunk.py`）：文档切片值对象，字段包括 id、document_id、ordinal、text、content_hash、token_count、metadata。

- EmbeddingVector（`app/domain/embedding.py`）：向量值对象，字段包括 chunk_id、model、dimensions、values。values 不写入 SQLite，不通过 HTTP API 返回。

`AttemptCost`（`app/domain/cost.py`）：单次 embedding 尝试的成本事实值对象（frozen dataclass，schema_version=1，与 N-Agent 同语义独立实现，不跨仓导入），字段包括 status（`estimated`/`unknown`）、currency（USD）、amount_usd（Decimal|None）、components、pricing_version、price_id、reason。配套 `AttemptUsage`（input 已知桶，output/cache_read/cache_write 由协议适配器声明 not_applicable）、`UnknownReason` 八值枚举、`PricingCatalogEntry`/`CatalogResolution` 与纯函数 `calculate_attempt_cost`（判定顺序与 N-Agent 相同）。索引批处理一次 HTTP 请求计一次，检索一次；缺计量即使零价仍 unknown。语义见 07-observability.md 模型成本语义章节。

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
- otel_enabled
- otel_exporter_otlp_endpoint
- otel_traces_sample_ratio
- otel_content_max_bytes
- otel_grafana_base_url
- otel_grafana_traces_base_url
- otel_grafana_metrics_base_url
- otel_grafana_logs_base_url
- otel_grafana_profile_base_url
- profiling_enabled
- profiling_pyroscope_endpoint
- profiling_interval_seconds

其中 otel_ / profiling_ 十二项为观测配置（env-only，启动期读取，无运行时修改接口）；exporter/Profile endpoint 强制 http/https、拒绝 userinfo/query/fragment；Grafana URL支持非秘密查询参数，拒绝userinfo/控制字符/秘密查询键及fragment，sample_ratio 限 0..1，interval 限 10..300 秒。新增三项外链优先于base_url派生值，单数profile命名与N-Agent一致。

定价目录配置（B 类 env-only，启动期加载一次、重启生效；不进 Dashboard）：

- pricing_catalog_path (str, 默认 "")：版本化价格目录 JSON 文件路径（`N_KB_PRICING_CATALOG_PATH`，默认部署 `/etc/n-kb/model-pricing.json`，只读挂载 `docker/observability/model-pricing.json`）。N-Agent 与 N-KB 部署相同内容的目录文件并核对同一预期哈希；出厂目录为空 prices。
- pricing_catalog_expected_sha256 (str, 默认 "")：目录内容的预期 SHA-256（`N_KB_PRICING_CATALOG_EXPECTED_SHA256`，64 位小写 hex）。哈希对目录 JSON 经 RFC 8785（JCS）规范化后的 UTF-8 字节计算；缺失按目录不可用处理（成本 pricing_unavailable），不符时目录失效，业务继续。

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
- 观测自检端点 `GET /observations/otel` 为无鉴权只读接口（page 1..10、size 固定 10，非法 422；`Cache-Control: no-store`），返回组合根注入的纯函数快照：status、mode、instance_id、sample_ratio、semconv_version、四信号投递状态、ring buffer spans 分页、分页元数据、Grafana 原外链与服务端四信号links、metric_snapshot、logs及逐Trace URL；不返回完整配置、凭据、原异常，不发起网络探测

## 观测数据边界

- ring buffer 快照（`app/infrastructure/observability/setup.py`）为内存 `deque(maxlen=100)`，保留已结束 sampled span 的八个基础字段及经注入精确白名单投影的安全attributes；组合根再补trace_url。不保存任意原始属性、事件、异常与Resource，无磁盘持久化
观测日志与详情采用统一Prepared脱敏和预算：注册模板用于稳定分类，普通应用/库/异常日志保留安全可读内容；Span属性/Event/OTLP复用同次投影，本地ring仅保留事实。完整文件、完整向量与凭据禁采，详见07-observability.md。
- span 属性白名单投影：`db.statement`/`db.query.text` 仅保留剥离字面量后的表名；`set_status` 剥离 description；`record_exception`/`add_event` 拦截；`error.type` 为低基数枚举，原始异常文本不进 span
- Profile 采样（py-spy）原始输出写系统临时目录（0700/0600、16 MiB 上限、成败均清理），上传前经折叠投影剥离动态帧与路径前缀，原始文件绝不上传，也不进入 `/app/data`

- 指标摘要复用15秒reader同批次，最多32 instrument×100点；日志最多100条、返回最新10条；精确安全字段/有限数值，短锁副本，关闭清空。完整形状与窗口/截断/状态说明见 `07-observability.md`。

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
