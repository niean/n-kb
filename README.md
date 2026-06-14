# N-KB

N-KB 是独立知识库/RAG 服务，对外提供稳定 HTTP API，对管理员提供本地 Management FE，用于管理知识库文档、标签、入库状态和检索效果。


## 关键设计

- 产品定位：独立知识库与 RAG 服务，负责文档管理、来源管理、标签分类、原文保存、入库索引、向量检索、检索 API 和本地管理页面。
- 管理入口：FastAPI 服务根路径 `/` 重定向到 `/static/index.html`，管理页支持概览、文档、检索和健康状态查看。
- API 入口：提供 health、文档管理、入库任务、语义检索等 HTTP API，外部调用方只依赖请求/响应语义。
- 文档类型：当前支持 Markdown 与纯文本上传，允许扩展到 PDF、网页、代码仓库等来源。
- 原文管理：保存上传原文件、原文、来源、标签、状态和 chunk 元数据；管理页可查看原文和只读 chunk，不支持在线编辑原文。
- 标签模型：统一使用 `key=value` 标签；分类是固定标签键 `category=<value>`，不引入独立分类字段，避免双写不一致。
- 元数据存储：SQLite 保存文档、原文、标签、chunk 元数据、入库任务和索引状态。
- 向量存储：Qdrant 保存 chunk 向量和检索 payload，Qdrant payload 是基础设施细节，不直接作为 API 合约返回。
- Embedding：Ollama + BGE-M3 生成文档 chunk embedding 和 query embedding。
- 文件存储：本地 ObjectStore 保存上传原文件，默认根目录为 `data/`，容器内路径为 `/app/data`。
- 部署方式：Docker Compose 是默认本地部署方式，服务包括 `n-kb`、`qdrant`、`ollama` 和 `ollama-pull-bge-m3`。

## 领域架构

N-KB 严格采用 DDD 分层，依赖方向为 Interfaces -> Application -> Domain。Infrastructure 实现 Domain 定义的端口，并由 `app/main.py` 在应用启动时统一注入。

```text
Interfaces ──> Application ──> Domain
     │              │
     └──────────────┴──> Domain ports <── Infrastructure
```

### Domain Layer

Domain 层定义业务模型和值对象，不依赖 FastAPI、SQLite、Qdrant、Ollama、LlamaIndex 或任何基础设施 SDK。

核心模型包括：

- `Document`：知识库文档聚合根，包含标题、来源、内容哈希、类型、大小、状态和时间戳。
- `DocumentSource`：文档来源，支持 upload、local_file、web、git、api 等来源类型。
- `DocumentContent`：原文内容，完整原文不得进入日志。
- `Tag`：统一 key-value 标签。
- `Chunk`：文档切片，包含序号、文本、哈希、token 数和元数据。
- `EmbeddingVector`：向量值对象，向量值不写入 SQLite，不通过 HTTP API 返回。
- `IndexJob`：入库/重建索引任务，记录状态、阶段和错误。
- `RetrievalQuery`、`RetrievalFilter`、`RetrievalResult`：检索请求、过滤条件和稳定检索结果。

Domain 端口包括 DocumentRepository、ChunkRepository、IndexJobRepository、ObjectStore、DocumentParser、TextSplitter、EmbeddingProvider 和 VectorIndex。

### Application Layer

Application 层编排业务用例，只依赖 Domain 模型和端口，不直接 import Infrastructure。

- `DocumentService`：处理上传、列表、详情、原文查看、标签更新、状态管理和删除。
- `IngestionService`：编排解析、切分、embedding、向量替换、chunk 元数据更新和任务状态。
- `RetrievalService`：编排 query embedding、过滤条件转换、向量检索和结果转换。
- `HealthService`：区分进程健康与依赖健康。

### Infrastructure Layer

Infrastructure 层实现外部依赖细节：

- `SQLiteStore`：实现文档、chunk、任务 repository，并初始化 SQLite schema。
- `LocalObjectStore`：保存上传原文件并处理路径安全。
- `QdrantVectorIndex`：实现向量写入、按文档替换、删除和检索。
- `OllamaEmbeddingProvider`：调用 Ollama embedding API。
- `SimpleTextParser`、`SimpleTextSplitter`：解析 Markdown/Text 并切分 chunk。

### Interfaces Layer

Interfaces 层负责 HTTP 协议和静态资源交付，不直接访问 SQLite、Qdrant、Ollama 或文件系统。

- FastAPI 路由：`/health`、`/health/dependencies`、文档管理、入库任务和检索接口。
- HTTP schema：定义稳定请求/响应模型，避免泄漏 SQLite row、Qdrant payload 或向量值。
- Management FE：静态页面和 JS/CSS 资源，用于文档上传、过滤、详情、chunk 查看、检索验证和依赖健康查看。

## 业务流程

### 文档上传与保存

1. 管理页或 HTTP 调用方提交 Markdown/Text 文件、来源信息和标签。
2. Interfaces 将请求转换为 Application command。
3. Application 校验文件扩展名、大小和标签格式。
4. ObjectStore 保存上传原文件。
5. DocumentRepository 保存文档元数据、原文、来源和标签。
6. 文档上传成功只代表原文和元数据已保存，不等于索引成功。

### 入库索引

1. 调用入库接口为文档创建 `IndexJob`。
2. Application 通过 DocumentParser 解析原文。
3. TextSplitter 将文本切分为 chunk。
4. EmbeddingProvider 为 chunk 生成向量。
5. Application 校验 chunk 数量、vector 数量和 chunk_id 对齐。
6. VectorIndex 使用 `replace_document(document_id, ...)` 按文档边界替换 Qdrant 向量。
7. SQLite 更新 chunk 元数据、文档状态和任务状态。
8. 若 Qdrant 写入失败，恢复该文档旧 chunk 元数据，并将文档/任务标记为失败。

### 文档管理

- 管理页可查看文档列表、来源、标签、状态、原文和只读 chunk。
- 标签过滤基于统一 `key=value` 模型，分类通过 `category=<value>` 表达。
- 原文和 chunk 默认按安全文本渲染，不拼接未转义 HTML。
- 删除文档时，Application 先确认文档存在，再清理 Qdrant 向量、ObjectStore 原文件和 SQLite 中的 document/content/tags/chunks/index_jobs。

### 语义检索

1. 调用方提交 query、filters、top_k 和可选 min_score。
2. RetrievalService 调用 Ollama 生成 query embedding。
3. Application 将 tags、source_kind、document_status 和 min_score 转换为 VectorIndex 过滤条件。
4. Qdrant 执行向量检索。
5. Application 将命中结果转换为稳定 `RetrievalResult`。
6. Interfaces 返回结构化 JSON，不透传 Qdrant 原始 payload 或向量值。

### Agent 接入

- N-Agent 或其它调用方通过 HTTP API 调用 N-KB。
- 调用方传入 query、过滤条件和调用上下文。
- N-KB 返回结构化检索结果，调用方自行决定如何注入到上层 Agent 上下文。

## 运行方式

### 本地 Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload --port 8212
```

默认配置从 `.env` 和环境变量读取，前缀为 `N_KB_`。常用配置：

```env
N_KB_SQLITE_PATH=locals/n-kb.db
N_KB_STORAGE_ROOT=data
N_KB_QDRANT_URL=http://localhost:6333
N_KB_QDRANT_COLLECTION=n_kb_documents
N_KB_EMBEDDING_BASE_URL=http://localhost:11434
N_KB_EMBEDDING_MODEL=bge-m3
N_KB_INGESTION_BATCH_SIZE=16
N_KB_MAX_UPLOAD_BYTES=2097152
N_KB_ALLOWED_FILE_EXTENSIONS=.md,.txt
```

### Docker Compose

```bash
docker compose -f docker/docker-compose.yml up --build
```

Compose 默认暴露：

- N-KB HTTP：`8212`
- Qdrant：`6333`
- Ollama：`11434`

## API 与页面入口

- Management FE：`/` 或 `/static/index.html`
- 进程健康：`/health`
- 依赖健康：`/health/dependencies`
- 文档管理：文档上传、列表、详情、原文、标签、chunk 查询和删除接口
- 入库任务：入库触发和任务状态查询接口
- 语义检索：检索请求和结构化结果接口

## 测试

```bash
python -m pytest -v
```

测试覆盖配置、DDD 边界、Domain 模型、Application 用例、SQLite、本地文件存储、文本解析、Qdrant adapter、Ollama provider、HTTP API、Management FE 安全渲染和 Docker Compose 配置。

## 项目结构

```text
app/
  main.py                         FastAPI 应用入口与依赖组装
  config.py                       N_KB_ 配置模型
  domain/                         领域模型与端口
  application/                    文档、入库、检索、健康用例
  infrastructure/                 SQLite、文件存储、Qdrant、Ollama、Parser 实现
  interfaces/http/                HTTP API、schema、错误映射和静态 FE
docker/
  Dockerfile                      服务镜像构建
  docker-compose.yml              本地 n-kb/qdrant/ollama 编排
tests/                            pytest 测试
```
