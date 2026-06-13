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
- 文档管理 API：`app/interfaces/http/documents.py`，实现上传、列表、详情、原文查看、标签查询和只读 chunk 查询
- 入库任务 API：`app/interfaces/http/indexing.py`，实现入库触发和任务状态查询
- 检索 API：`app/interfaces/http/retrieval.py`，实现语义检索接口
- HTTP schema：`app/interfaces/http/schemas.py`，定义稳定请求/响应模型和检索输入边界
- HTTP 错误映射：`app/interfaces/http/errors.py`，将应用错误转换为安全、稳定的错误 payload
- MCP 检索适配：`app/interfaces/mcp/server.py`，使用 FastMCP Streamable HTTP 暴露 `search_knowledge` tool，只支持 `streamable_http` 不支持 `sse`，复用 `RetrievalService.search` 并过滤公开结果
- FE 静态页面入口：`app/interfaces/http/static/index.html`，定义 Dashboard-first 管理页 app shell、sidebar、topbar 和总览/文档/检索/健康容器
- FE 视觉样式：`app/interfaces/http/static/styles.css`，定义管理页 Design Token、布局、表单、按钮、Badge、状态和导航样式
- FE API 模块：`app/interfaces/http/static/management-api.js`，封装管理页 HTTP API 调用，不直接访问后端基础设施
- FE UI 工具模块：`app/interfaces/http/static/management-ui.js`，封装安全文本渲染、Badge、空/加载/错误状态和标签解析 helper
- FE 导航模块：`app/interfaces/http/static/management-navigation.js`，管理 sidebar 展开、hash 路由、导航高亮和顶栏标题同步
- FE 页面入口脚本：`app/interfaces/http/static/app.js`，编排文档、检索、健康页面渲染和事件绑定

## Docker 与部署

- 镜像构建：`docker/Dockerfile`，使用 Python 3.11 slim 镜像，安装项目并以 Uvicorn 启动服务端口
- Compose 部署：`docker/docker-compose.yml`，定义 `n-kb`、`qdrant`、`ollama`、`ollama-pull-bge-m3` 服务、端口和 volume
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
- 本地文件存储测试：`tests/infrastructure/test_local_object_store.py`
- 文本 parser/splitter 测试：`tests/infrastructure/test_text_parser.py`
- Qdrant adapter 测试：`tests/infrastructure/test_qdrant_index.py`
- Ollama provider 测试：`tests/infrastructure/test_ollama_provider.py`
- HTTP API 测试：`tests/interfaces/test_health_api.py`、`tests/interfaces/test_documents_api.py`、`tests/interfaces/test_retrieval_api.py`
- MCP 接口测试：`tests/interfaces/test_mcp_server.py`，覆盖 MCP tool 复用检索服务、输入校验、错误遮蔽、tool 暴露范围和 FastAPI 挂载
- FE 安全渲染测试：`tests/interfaces/test_management_fe.py`

## Harness 任务文件

- 设计 spec：`.harness/specs/active/`
- 实现 plan：`.harness/plans/active/`
- 架构知识：`.harness/knowledge/02-architecture.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`
