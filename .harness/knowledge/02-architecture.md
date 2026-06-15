<!-- SUMMARY: N-KB 的 DDD 架构边界、RAG 服务边界、依赖方向和核心模块原则 -->
# 架构与模块边界

## 架构定位

N-KB 是独立知识库与 RAG 产品，不是 N-Agent 的内部模块。N-Agent 是 N-KB 的 HTTP 调用方之一，不能直接依赖 N-KB 的内部 Python 包、数据库表或向量库集合结构。

N-KB 的目标是提供完整知识库产品能力：文档管理、来源管理、标签分类、原文保存、索引构建、向量检索、检索 API、简单 FE 管理和本地 Docker Compose 部署。

## 分层

项目严格遵循领域驱动设计 DDD，采用外层依赖内层的方向：Interfaces -> Application -> Domain。Infrastructure 只实现 Domain 定义的端口，并在应用启动时注入。

- Domain 层：定义 Document、KnowledgeSource、Tag、Chunk、EmbeddingVector、IndexJob、RetrievalQuery、RetrievalResult 等核心领域模型和值对象，定义 DocumentRepository、SourceRepository、TagRepository、ChunkRepository、VectorIndex、EmbeddingProvider、DocumentParser、ObjectStore 等端口协议。
- Application 层：编排文档上传、原文保存、入库索引、标签过滤、文档查询、语义检索和管理用例。LangGraph 属于本层，只负责状态图和流程编排。
- Infrastructure 层：实现外部依赖细节，包括 SQLite repository、Qdrant vector index、Ollama embedding provider、LlamaIndex parser/index adapter、本地文件存储、配置加载等。
- Interfaces 层：实现 FastAPI HTTP API、FE 静态资源、请求/响应模型和错误映射。

## 模块边界

- Domain 不依赖 FastAPI、LangGraph、SQLite、Qdrant SDK、Ollama HTTP client、LlamaIndex 或任何 Infrastructure 具体实现。
- Application 依赖 Domain 模型和端口，可以使用 LangGraph 表达流程，但不得 import `app.infrastructure`。
- Infrastructure 可以依赖 Domain 端口并实现它们，不能反向要求 Domain 了解具体存储、向量库、Embedding 服务或 LlamaIndex 类型。
- Interfaces 只调用 Application services，只做请求/响应、静态资源交付和协议转换。
- Interfaces 不直接访问 SQLite、Qdrant、Ollama、LlamaIndex，不承载入库、切分、索引、检索排序等业务规则。
- 应用启动入口负责依赖组装，将 Infrastructure 实现注入 Application 服务。

## 核心模块

- Document Management：管理文档原文件、原文、来源、标签、分类标签、状态和审计时间，不提供在线编辑。
- Ingestion Workflow：负责上传后解析、chunk、embedding、向量写入和索引状态更新，可用 LangGraph 编排。
- Retrieval Workflow：负责 query embedding、过滤条件转换、Qdrant 检索、结果排序和输出转换。
- RAG Adapter：通过 LlamaIndex 编排文档加载、切分和检索辅助能力，但 LlamaIndex 类型不进入 Domain。
- Embedding Adapter：通过 Domain `EmbeddingProvider` 端口屏蔽 Ollama+BGE-M3 细节。
- Vector Index：通过 Domain `VectorIndex` 端口屏蔽 Qdrant collection、payload 和 SDK 细节。
- Metadata Store：通过 repository 端口屏蔽 SQLite schema 和 SQL 细节。
- Management FE：服务本地文档上传、列表、详情、标签过滤、索引状态查看和检索验证。
- Public HTTP API：服务 N-Agent 和其它调用方，提供稳定的文档、标签、入库、检索接口。
- MCP Interface Adapter：作为 Interfaces 层协议适配，通过 FastMCP 暴露检索与状态 tools，支持 Streamable HTTP 和 stdio 双入口，复用 Application 层检索服务和健康检查服务，不直接访问 Infrastructure；N-KB 不支持旧版 SSE transport。

## 服务边界

- N-KB 对外只承诺 HTTP API 和响应语义，不承诺内部 SQLite schema、Qdrant payload、Python 类或 LlamaIndex 配置。
- N-Agent 调用 N-KB 时只传 query、过滤条件和调用方上下文，不直接传递内部 AgentState。
- Qdrant 与 Ollama 是 N-KB 的基础设施依赖，可由 N-KB Docker Compose 管理，也可配置为外部服务。
- N-KB 可独立启动、独立测试、独立部署、独立演进。

## 演进边界

后续完整产品能力应在现有边界内迭代，不推倒重来。优先演进方向包括：

1. 多文件类型解析：PDF、HTML、网页、代码仓库、结构化数据。
2. 多来源连接器：上传、目录同步、Git、网页抓取、API 导入。
3. 标签体系增强：key-value 标签、批量管理、过滤表达式、自动标签建议。
4. 索引任务平台：后台队列、失败重试、进度事件、重建索引、增量更新。
5. 检索增强：hybrid search、rerank、多 collection、多租户或 workspace。
6. 调用方治理：API key、权限、审计、配额和调用日志。
7. 观测能力：检索命中分析、embedding/向量库健康检查、任务指标。

单次实现计划不得一次性展开所有能力；但领域模型、端口和模块边界必须支持这些方向。
