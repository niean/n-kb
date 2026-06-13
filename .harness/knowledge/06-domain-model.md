<!-- SUMMARY: N-KB 的 DDD 领域模型速览，帮助技术人员快速理解限界上下文、聚合、值对象、端口与核心业务流 -->
# 领域模型速览

## 目标

N-KB 是一个独立的知识库与 RAG 服务。它的核心职责不是“生成答案”，而是稳定地管理知识文档、构建索引，并向调用方返回可用的检索结果。

从 DDD 视角看，项目的核心领域问题是：

- 如何把外部文档变成可管理、可追踪、可检索的知识资产
- 如何在不暴露底层存储细节的前提下，对外提供稳定的检索能力
- 如何保证文档元数据、chunk 元数据和向量索引在生命周期内保持一致

## 限界上下文

N-KB 是独立上下文，不是 N-Agent 的内部模块。

- N-KB 对外提供 HTTP API
- N-Agent 只是调用方之一，不直接访问 Python 包、SQLite schema 或 Qdrant payload
- SQLite、Qdrant、Ollama 都属于基础设施，不属于领域模型本身

因此，Domain 层只表达“文档如何被管理和检索”，不表达具体 SDK、数据库表或第三方服务协议。

## 核心聚合

### Document 聚合

`Document` 是当前最核心的聚合根，代表一个被纳入知识库管理的文档。

它负责承载这些业务事实：

- 文档标识：`id`
- 标题：`title`
- 来源：`source`
- 内容摘要信息：`content_hash`、`content_type`、`size_bytes`
- 生命周期状态：`uploaded`、`indexing`、`indexed`、`failed`、`deleted`
- 审计时间：`created_at`、`updated_at`

与 `Document` 强关联的对象有：

- `DocumentSource`：来源值对象，描述文档从哪里来
- `DocumentContent`：原文内容值对象，保存可解析文本
- `Tag`：文档标签值对象，用于分类和过滤

可以把它理解为“知识文档管理”的事务边界。上传、状态变更、删除都围绕 `Document` 聚合发生。

### IndexJob

`IndexJob` 不是文档聚合的一部分，而是独立的流程跟踪对象，用来表示一次索引任务的执行状态。

它关注的是：

- 任务属于哪个文档
- 当前处于哪个阶段：`created`、`parsing`、`splitting`、`embedding`、`writing_vector_index`、`completed`、`failed`
- 任务是否成功
- 失败时保留什么安全错误摘要

它的意义是把“文档存在”与“索引是否成功”分开表达。

## 关键值对象

- `DocumentSource`：来源信息，当前上传场景使用 `kind / uri / display_name / metadata`
- `DocumentContent`：原文文本与编码信息，是解析和切分的输入
- `Tag`：统一 `key=value` 标签模型，`category` 只是固定键，不单独建树
- `Chunk`：切分后的文本片段，是向量化和检索的最小业务单位
- `EmbeddingVector`：chunk 对应的向量结果，保留 `chunk_id` 用于一致性校验
- `RetrievalQuery`：检索请求，包含 query、filters、top_k、min_score
- `RetrievalFilter`：标签、来源、文档状态等过滤条件
- `RetrievalResult`：检索结果，面向应用层和接口层暴露稳定结构

这些对象共同表达了“从文档到检索结果”的业务链路。

## 领域端口

Domain 层通过端口描述能力边界，而不绑定实现。

仓储端口：

- `DocumentRepository`
- `ChunkRepository`
- `IndexJobRepository`

能力端口：

- `ObjectStore`
- `DocumentParser`
- `TextSplitter`
- `EmbeddingProvider`
- `VectorIndex`

设计重点是：Application 只依赖这些端口编排用例，Infrastructure 再去实现 SQLite、Qdrant、Ollama 和本地文件存储。

## 主要业务流

### 1. 文档上传

`DocumentService.upload_document` 负责：

- 校验扩展名和大小
- 解析标签
- 基于内容 hash 去重
- 保存原文件
- 创建 `Document`、`DocumentContent` 和标签记录

这里的结果是“文档被系统接收”，不是“文档已经可检索”。

### 2. 文档入库

`IngestionService.index_document` 负责把文档变成可检索资产：

1. 读取 `Document` 与 `DocumentContent`
2. 创建 `IndexJob`
3. 调用 `DocumentParser` 解析文本
4. 调用 `TextSplitter` 生成 `Chunk`
5. 调用 `EmbeddingProvider` 生成 `EmbeddingVector`
6. 校验 chunk 与向量一一对应
7. 调用 `VectorIndex.replace_document` 写入向量库
8. 更新文档状态和任务状态

这条链路体现了项目最重要的领域规则：元数据、chunk 与向量索引必须保持对齐。

### 3. 语义检索

`RetrievalService.search` 负责：

- 将 query 转成向量
- 应用标签、来源、状态和相关性过滤
- 从 `VectorIndex` 查询结果
- 返回稳定的 `RetrievalResult`

这里的核心不是向量库查询本身，而是把底层检索细节收敛成可对外承诺的领域结果。

### 4. 文档删除

`DocumentService.delete_document` 负责：

- 先确认文档存在
- 清理该文档对应的向量索引
- 删除原文件
- 删除文档、原文、标签、chunk 和索引任务记录

这说明删除也是一个聚合级操作，目标是保持多存储边界的一致性。

## 当前模型的简化点

为了保持实现简洁，当前领域模型做了几项有意识的简化：

- 只有一个核心聚合根：`Document`
- 标签使用统一 key-value，而不是独立分类树
- chunk 目前是围绕文档生命周期管理的从属对象
- `IndexJob` 只做流程追踪，不承载复杂调度语义
- 检索结果直接以 `RetrievalResult` 作为对外稳定模型，不暴露 rerank、召回策略等细节

这些简化适合当前单机、本地优先、RAG 服务型产品的阶段目标。

## 演进方向

如果后续能力继续扩展，领域上大概率会沿这些方向演进：

- 从单一上传来源扩展到目录同步、Git、网页、API 导入
- 从简单标签扩展到更复杂的过滤表达式或批量标签治理
- 从同步索引扩展到后台任务、重试、增量更新
- 从单一向量检索扩展到 hybrid search、rerank、多租户或多 workspace

无论怎么演进，DDD 上最重要的约束不变：

- Domain 保持纯净
- Application 只编排用例
- Infrastructure 隔离外部依赖
- Interfaces 只做协议转换

## 快速理解代码时的阅读顺序

建议按下面顺序阅读：

1. `app/domain/document.py`
2. `app/domain/chunk.py`
3. `app/domain/tag.py`
4. `app/domain/retrieval.py`
5. `app/domain/repositories.py`
6. `app/domain/ports.py`
7. `app/application/document_service.py`
8. `app/application/ingestion_service.py`
9. `app/application/retrieval_service.py`
10. `app/infrastructure/persistence/sqlite_store.py`
11. `app/infrastructure/vector/qdrant_index.py`
12. `app/main.py`

读完这条链路，基本就能建立起项目当前的领域模型全貌。
