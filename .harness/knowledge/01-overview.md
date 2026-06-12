<!-- SUMMARY: N-KB 是独立知识库与 RAG 服务，使用 Python/FastAPI/LangGraph/LlamaIndex/SQLite/Qdrant/Ollama，通过 Docker Compose 本地部署 -->
# 项目概览

## 一句话

N-KB 是面向本地开发者和 Agent 调用方的独立知识库与 RAG 服务，提供文档上传、原文管理、标签分类、向量索引、语义检索、HTTP API 和简单 FE 管理能力。

## 技术栈

- 语言与版本：Python 3.11+
- Web 框架：FastAPI、Uvicorn
- 工作流编排：LangGraph，位于 Application 层，用于表达入库、索引、检索等可演进流程
- RAG 编排：LlamaIndex，位于 Infrastructure 适配层或 Application 编排边界之后，不污染 Domain 模型
- 向量数据库：Qdrant，保存 chunk 向量与检索 payload
- Embedding：Ollama + BGE-M3，通过 Ollama HTTP API 生成向量
- 元数据存储：SQLite，保存文档、来源、原文、标签、chunk 元数据、入库任务和索引状态
- 配置：pydantic-settings，从 `.env` 和环境变量读取，前缀为 `N_KB_`
- 测试：pytest、pytest-asyncio、httpx/TestClient
- 部署：Dockerfile + Docker Compose

## 入口与根状态

- 应用入口：`app/main.py`，提供 `create_app(settings: Settings | None = None)` 和模块级 `app`
- 配置入口：`app/config.py`，定义 `Settings`
- HTTP API：`app/interfaces/http/`，提供 health、文档管理、入库、检索、标签和调用方 API
- FE 入口：`app/interfaces/http/static/`，提供简单知识库管理页面
- 依赖组装：`create_app` 组装 Settings、SQLite store、Qdrant store、Ollama embedding provider、LlamaIndex adapter、Application services 和 HTTP routes

## 核心流程

1. 文档上传：用户通过 FE 或 HTTP API 上传 Markdown/Text 文档，并提交来源、标签和分类标签。
2. 原文保存：Application 校验输入后通过 Domain 端口保存原文件、原文和元数据到 SQLite 或文件存储。
3. 入库索引：Application 编排文档解析、chunk 生成、embedding 生成和 Qdrant 写入，记录任务状态。
4. 文档管理：FE/API 查询文档列表、详情、来源、标签、原文和索引状态；不提供在线编辑原文。
5. 语义检索：调用方提交 query 和过滤条件，服务生成 query embedding，查询 Qdrant，并返回结构化检索结果。
6. Agent 接入：N-Agent 或其它调用方通过 HTTP API 使用检索结果，不直接 import n-kb 内部代码。

## 部署与运行

Docker Compose 是默认运行方式。推荐 `.env` 中使用容器内路径：

```env
COMPOSE_PROJECT_NAME=n-kb
N_KB_SQLITE_PATH=/app/locals/n-kb.db
N_KB_STORAGE_ROOT=/app/data
N_KB_QDRANT_URL=http://qdrant:6333
N_KB_QDRANT_COLLECTION=n_kb_documents
N_KB_EMBEDDING_BASE_URL=http://ollama:11434
N_KB_EMBEDDING_MODEL=bge-m3
```

默认服务应包含：
- `n-kb`：FastAPI 服务
- `qdrant`：向量数据库
- `ollama`：本地 embedding 服务，也可配置为外部已存在的 Ollama

## 文档与规则

- 操作约束见 `.harness/framework/FRAMEWORK.md`
- 项目配置见 `.harness/PROJECT.md`
- DDD 架构边界见 `.harness/knowledge/02-architecture.md`
- 实现约定见 `.harness/knowledge/03-conventions.md`
- 数据与存储边界见 `.harness/knowledge/04-data-boundaries.md`
- 文件职责见 `.harness/knowledge/22-file-map.md`
