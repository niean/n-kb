<!-- SUMMARY: N-KB 的实现约定，包括 Python/DDD 分层、RAG 适配、测试、错误处理、安全、Docker Compose 和文件管理规则 -->
# 约定与约束（实现细节）

本文件是项目实现规范约定的权威来源，`.harness/PROJECT.md` "项目规范"各节为摘要引用，以本文件为准。

---

# 一、编码约定

## Python 与包结构

- Python 版本：3.11+
- 应用包：`app/`
- 测试包：`tests/`
- 源码按 DDD 分层目录组织：`domain/`、`application/`、`infrastructure/`、`interfaces/`
- `app/main.py` 是唯一负责组装 Infrastructure 具体实现的位置
- 新增业务能力时优先在 Domain 定义模型/端口，再由 Application 编排，最后由 Infrastructure/Interfaces 实现外部细节

## DDD 依赖方向

- Domain 层禁止 import FastAPI、LangGraph、SQLite、Qdrant SDK、Ollama SDK/HTTP client、LlamaIndex 或 `app.infrastructure`
- Application 层禁止 import `app.infrastructure`
- Interfaces 层禁止 import SQLite、Qdrant SDK、Ollama client、LlamaIndex 和任何 `app.infrastructure` 模块
- Infrastructure 可以 import Domain 端口并实现它们
- Interfaces 只调用 Application services，不直接执行 parser、embedding、vector index 或 repository handler
- DDD 边界必须由静态测试守护

## HTTP API

- 对外 API 位于 Interfaces 层，仅作为外部协议适配，不作为内部领域模型
- 请求字段可接受产品定义的常见字段；未知字段默认拒绝或显式忽略，不能悄悄影响检索行为
- API 响应使用稳定 JSON 结构，内部 SQLite row、Qdrant payload、LlamaIndex 对象不得直接返回
- 检索 API 返回结果应包含 document_id、chunk_id、score、snippet、source、tags、metadata，不返回底层向量
- Provider 或基础设施调用失败时，返回统一错误 payload，不泄漏密钥、完整原文或内部堆栈

## LangGraph

- LangGraph 只存在于 Application 层
- 可用于编排 `validate_document -> parse_document -> split_chunks -> embed_chunks -> write_vector_index -> update_index_state`
- 可用于编排 `normalize_query -> embed_query -> search_vector_index -> map_results -> finalize`
- Domain 模型不得暴露 LangGraph 类型
- 简单用例不强制使用 LangGraph，只有流程状态或可恢复性需要时再引入

## LlamaIndex

- LlamaIndex 是代码框架，不是独立服务
- LlamaIndex 使用边界在 Infrastructure adapter 或 Application 编排之后，不能污染 Domain 模型
- LlamaIndex node、document、retriever、index 对象不得作为 Domain 端口入参或返回值
- Domain 只认识 Document、Chunk、EmbeddingVector、RetrievalResult 等项目自有模型
- 替换 LlamaIndex 不应影响 Interfaces/API 和 Domain 模型

## RAG 外部依赖

- Qdrant 只通过 Domain `VectorIndex` 端口访问
- Ollama+BGE-M3 只通过 Domain `EmbeddingProvider` 端口访问
- SQLite 只通过 repository 端口访问
- 外部依赖的 base URL、collection、model、timeout、batch size 均来自 Settings
- 测试中允许用 fake port 实现替代真实外部服务；不要测试 mock 自身行为

---

# 二、配置约定

## 环境变量

配置模型位于 `app/config.py`，使用 `N_KB_` 前缀：

- `N_KB_SQLITE_PATH`
- `N_KB_STORAGE_ROOT`
- `N_KB_QDRANT_URL`
- `N_KB_QDRANT_COLLECTION`
- `N_KB_EMBEDDING_BASE_URL`
- `N_KB_EMBEDDING_MODEL`
- `N_KB_INGESTION_BATCH_SIZE`
- `N_KB_MAX_UPLOAD_BYTES`

Docker Compose 项目隔离使用：

- `COMPOSE_PROJECT_NAME=n-kb`

## Docker Compose 默认值

只考虑 Docker Compose 运行时，推荐容器内路径：

- SQLite：`/app/locals/n-kb.db`
- 原文件与派生产物：`/app/data`
- HTTP 服务端口：优先使用 `8212:8212`
- Qdrant REST：`6333:6333`
- Qdrant gRPC：`6334:6334`
- Ollama API：容器内默认 `11434`
- Ollama+BGE-M3 本地 compose 运行通过一次性 bootstrap 服务拉取配置模型，业务服务依赖该服务成功完成后启动

宿主机目录通过 `docker/docker-compose.yml` volume 映射到容器路径，避免容器内状态丢失。本项目面向 Docker Desktop 本地访问时使用端口映射，不使用 `network_mode: host`。

## 密钥

- `.env` 可存放本地真实 Provider API Key 或服务凭据，但不得提交
- `.env.example` 只保留占位值或空值，不写真实密钥
- 任何日志、测试和文档都不得输出真实 API Key
- `docker compose config` 会展开 `.env`，输出不得公开粘贴

---

# 三、质量约定

## 测试

- 测试命令：`python -m pytest -v`
- 新增 Domain、Application、Infrastructure、Interfaces 能力时必须补对应测试
- 涉及 DDD 边界变更时必须运行架构边界测试
- 涉及 Docker Compose 变更时必须运行 Docker Compose 配置校验
- 涉及 SQLite schema 变更时必须测试初始化、读写和迁移兼容边界
- 涉及 RAG 链路时必须测试文档入库、chunk 生成、embedding provider 边界、vector index 边界和检索结果映射

## 错误处理

- 上传文件不支持：返回 `unsupported_file_type`
- 上传文件过大：返回 `file_too_large`
- 标签格式非法：返回 `invalid_tags`
- 文档不存在：返回 `document_not_found`
- 索引任务失败：记录失败状态和安全错误摘要，不丢失原文
- Embedding 调用失败：任务失败或检索失败，错误摘要不得包含完整原文
- Qdrant 写入/查询失败：返回基础设施错误，不伪造空结果
- SQLite 写入失败：当前请求失败，不静默丢状态

## 验收命令

```bash
python -m pytest -v
docker compose -f docker/docker-compose.yml config
curl http://127.0.0.1:8212/health
```

---

# 四、文件管理约定

- 不主动创建 README，除非用户明确要求
- 不自主删除项目文件
- 文件名使用小写英文 kebab-case
- Python 模块名使用 snake_case
- `locals/`、`logs/`、`data/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.egg-info/` 是本地运行、测试或构建缓存产物，应由 `.gitignore` 忽略，不需要提交
- 部署相关文件统一放在 `docker/` 目录；真实本地配置不得包含密钥
- `.harness/prd/` 是 AI-READONLY，不能自动修改
- `.harness/knowledge/` 是实现后知识回填目标，可按 Harness 流程更新

---

# 五、安全约定

- 上传文件默认只能写入配置的 storage 根目录
- 文件路径必须通过真实路径解析限制在 storage 根目录内，拒绝路径穿越和软链接逃逸
- Markdown/Text 原文可能包含密钥或私有信息，日志不得输出完整原文
- 检索结果 snippet 应限制长度，避免一次性泄漏过多原文
- FE 使用安全文本渲染，不通过拼接 HTML 注入文档标题、标签、来源或检索片段
- 管理页前端组件、布局、视觉和交互规范见 `.harness/framework/guides/10-guidelines-fe.md`
- Shell、任意文件写入、远程抓取等能力不属于默认可公开能力，必须通过后续权限和风险控制设计启用
- Docker Compose 挂载目录不应指向过大的敏感目录
- Dockerfile 与 Compose 外部服务镜像应固定到具体版本或 digest；基础运行镜像优先使用 patch tag + sha256 digest
