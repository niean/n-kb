<!-- SUMMARY: N-KB 当前规划源码、测试、配置、Docker 部署和 Harness 任务文件的职责映射 -->
# 功能与文件映射

## 应用入口与配置

- FastAPI 应用入口：`app/main.py`，提供 `create_app`，组装 Infrastructure 具体实现并注册 HTTP/FE 路由
- 配置模型：`app/config.py`，定义 `Settings`，从 `.env` 和环境变量读取 `N_KB_` 配置
- Python 依赖：`pyproject.toml`，定义运行依赖、dev 依赖和 pytest 配置
- 环境变量模板：`.env.example`，不包含真实密钥

## Domain Layer

- 文档领域模型：`app/domain/document.py`，定义 Document、DocumentSource、DocumentContent、DocumentStatus
- 标签领域模型：`app/domain/tag.py`，定义 Tag 和标签解析/校验规则
- Chunk 领域模型：`app/domain/chunk.py`，定义 Chunk 和 chunk 元数据
- Embedding 领域模型：`app/domain/embedding.py`，定义 EmbeddingVector、EmbeddingProvider
- 索引领域模型：`app/domain/indexing.py`，定义 IndexJob、IndexJobStatus、IndexStage
- 检索领域模型：`app/domain/retrieval.py`，定义 RetrievalQuery、RetrievalResult、RetrievalFilter
- Repository 端口：`app/domain/repositories.py`，定义 DocumentRepository、ChunkRepository、IndexJobRepository
- 存储与解析端口：`app/domain/ports.py`，定义 ObjectStore、DocumentParser、TextSplitter、VectorIndex

## Application Layer

- 文档管理用例：`app/application/document_service.py`，编排上传、查询、详情、标签更新和状态管理
- 入库索引用例：`app/application/ingestion_service.py`，编排解析、切分、embedding、向量写入和任务状态
- 检索用例：`app/application/retrieval_service.py`，编排 query embedding、过滤、向量检索和结果转换
- 健康检查用例：`app/application/health_service.py`，区分进程健康和依赖健康
- 入库工作流：`app/application/ingestion_graph.py`，在需要时使用 LangGraph 表达入库状态图

## Infrastructure Layer

- SQLite store：`app/infrastructure/persistence/sqlite_store.py`，实现文档、chunk、任务 repository 并初始化 schema
- 本地文件存储：`app/infrastructure/storage/local_object_store.py`，实现 ObjectStore 和路径安全
- Qdrant VectorIndex：`app/infrastructure/vector/qdrant_index.py`，实现向量写入、删除和检索
- Ollama EmbeddingProvider：`app/infrastructure/embedding/ollama_provider.py`，调用 Ollama `/api/embed`
- LlamaIndex adapter：`app/infrastructure/rag/llama_index_adapter.py`，实现解析、切分或检索辅助适配
- 文本 parser：`app/infrastructure/parsers/text_parser.py`，支持 Markdown/Text 原文解析

## Interfaces Layer

- HTTP 路由入口：`app/interfaces/http/routes.py`，汇总注册路由
- 健康检查 API：`app/interfaces/http/health.py`，实现 `/health`、`/health/dependencies`
- 文档管理 API：`app/interfaces/http/documents.py`，实现上传、列表、详情、原文查看、标签查询
- 入库任务 API：`app/interfaces/http/indexing.py`，实现入库触发、任务状态、重建索引
- 检索 API：`app/interfaces/http/retrieval.py`，实现语义检索接口
- FE 静态页面：`app/interfaces/http/static/`，提供文档上传、列表、详情、标签过滤和检索验证页面

## Docker 与部署

- 镜像构建：`Dockerfile`，使用 Python 3.11 slim 镜像，安装项目并以 Uvicorn 启动服务端口
- Compose 部署示例：`docker-compose.yml.example`，定义 `n-kb`、`qdrant`、可选 `ollama` 服务、端口和 volume
- Docker 构建忽略：`.dockerignore`，排除 `.claude`、`.harness`、`.git`、缓存、venv、locals、data
- 本地运行产物：`locals/`、`data/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.egg-info/` 是运行、测试或构建缓存产物

## 测试

- 配置测试：`tests/test_config.py`
- DDD 边界测试：`tests/test_architecture_boundaries.py`
- Docker Compose 配置测试：`tests/test_docker_compose_config.py`
- Domain 模型测试：`tests/domain/test_models.py`
- 文档管理用例测试：`tests/application/test_document_service.py`
- 入库索引用例测试：`tests/application/test_ingestion_service.py`
- 检索用例测试：`tests/application/test_retrieval_service.py`
- SQLite store 测试：`tests/infrastructure/test_sqlite_store.py`
- Qdrant adapter 测试：`tests/infrastructure/test_qdrant_index.py`
- Ollama provider 测试：`tests/infrastructure/test_ollama_provider.py`
- HTTP API 测试：`tests/interfaces/test_documents_api.py`、`tests/interfaces/test_retrieval_api.py`
- FE 安全渲染测试：`tests/interfaces/test_management_fe.py`

## Harness 任务文件

- 设计 spec：`.harness/specs/active/`
- 实现 plan：`.harness/plans/active/`
- 架构知识：`.harness/knowledge/02-architecture.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`
