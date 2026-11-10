<!-- SUMMARY: N-KB 当前规划源码、测试、配置、Docker 部署和 Harness 任务文件的职责映射；模型成本观测（domain/cost.py + embedding/pricing_catalog.py + embedding 计价生命周期/遥测接线 + 版本化定价目录与 E2E 成本 fixtures） -->
# 功能与文件映射

## 应用入口与配置

- FastAPI 应用入口：`app/main.py`，提供 `create_app`，注册 HTTP/FE 路由并挂载可选 MCP HTTP 入口；负责 FastAPI lifespan 中观测启动、业务服务组装、MCP session 与句柄释放
- stdio 入口：`app/mcp_stdio.py`，组装 stdio MCP 服务与观测启动，Infrastructure 实现经组合根函数注入
- 依赖组装：`app/composition.py`，集中组装 Infrastructure 具体实现、Application services 和健康检查服务；含观测装配 `start_observability`、业务日志事件登记、自检快照 callable 构造、client span 抑制解析与 embedding 计价闭包注入（`make_embedding_cost_calculator`：加载版本化目录，unavailable 降级 pricing_unavailable，不阻断业务）
- 配置模型：`app/config.py`，定义 `Settings`，从 `.env` 和环境变量读取 `N_KB_` 配置（含十二个 otel_ / profiling_ 观测配置与定价目录 pricing_catalog_path/pricing_catalog_expected_sha256，均为 env-only 启动期绑定）
- Python 依赖：`pyproject.toml`，定义运行依赖、dev 依赖和 pytest 配置
- 环境变量模板：`docker/.env.example`，不包含真实密钥和宿主机个人路径

## Domain Layer

- 文档领域模型：`app/domain/document.py`，定义 Document、DocumentSource、DocumentContent、DocumentStatus
- 标签领域模型：`app/domain/tag.py`，定义 Tag 和标签解析/校验规则
- Chunk 领域模型：`app/domain/chunk.py`，定义 Chunk 和 chunk 元数据
- Embedding 领域模型：`app/domain/embedding.py`，定义 EmbeddingVector、EmbeddingProvider
- 模型成本领域模型：`app/domain/cost.py`，定义 `AttemptUsage`（input 桶 + not_applicable 声明）、`AttemptCost`（estimated/unknown 不变量）、`UnknownReason` 8 值枚举、`PricingCatalogEntry`/`CatalogResolution` 与纯函数 `calculate_attempt_cost`；与 N-Agent 同语义独立实现，不跨仓导入；纯领域无 SDK 依赖
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
- 观测固定契约：`app/application/telemetry_contract.py`，仅标准库；不可变对象、操作、结果、事件、严格属性类型、十一项内容与十六项 instrument（含 3 个成本 Counter `nkb.model.cost/cost_components/cost_attempts`）；通用执行投影拒绝内容；模型对象登记 `nkb.model.cost_status/cost_reason/cost_currency/cost_decimal/cost_input_usd/pricing_version/price_id/billing_provider/billing_model` 成本属性键
- 观测语义层：`app/application/telemetry_semantics.py`，标准库与 OTel API；管理操作生命周期、冻结完成快照、一次测量及 Span/Event/Log 发布，提供 `embedding_observer`（保留 raw_model 精确查价 + 安全 model 标签，finalization 计算/写入 AttemptCost：Span 属性 + `nkb.model.completed` 成本事实）与受信完成回调；`_measure_completion` 发布 3 个 `nkb.model.cost*` Counter；计价闭包经 composition 注入，Infrastructure 不 import Application
- 请求归属：`app/application/telemetry_requests.py`，独立请求 owner、受信入口 channel 及同步路由线程池结果回传，退出恢复、并发隔离

## Infrastructure Layer

- SQLite store：`app/infrastructure/persistence/sqlite_store.py`，实现文档、chunk、任务 repository 并初始化 schema
- 本地文件存储：`app/infrastructure/storage/local_object_store.py`，实现 ObjectStore 和路径安全
- Qdrant VectorIndex：`app/infrastructure/vector/qdrant_index.py`，实现向量写入、删除和检索
- Ollama EmbeddingProvider：`app/infrastructure/embedding/ollama_provider.py`，调用 Ollama `/api/embed`；成功响应且 data 含合法计量字段后先 `record_tokens` 再解析向量（计量先行），解析失败保留已知 Token 与可计算费用，缺计量记 None -> unknown；output/cache_* 声明 not_applicable
- Embedding 定价目录：`app/infrastructure/embedding/pricing_catalog.py`，独立 `JsonPricingCatalog` 目录 loader/resolver（JCS 内容哈希 + 预期哈希核对、price_id 唯一、`(billing_provider, billing_model, operation)` 精确匹配 + aliases），与 N-Agent 共用 fixture 契约而非跨仓 import
- LlamaIndex adapter：`app/infrastructure/rag/llama_index_adapter.py`，实现解析、切分或检索辅助适配
- 文本 parser：`app/infrastructure/parsers/text_parser.py`，支持 Markdown/Text 原文解析

### 观测（OTel）

- HTTP采集：`app/infrastructure/observability/http_capture.py`，有界请求/回执byte tap、协议内容策略与独立Exchange现场，复用instrumentors原生命周期

- 详情投影与安全现场：`app/application/telemetry_details.py`，纯标准库、固定详情信封、共享预算和独立ContextVar作用域
- RAG详情摘要：`app/application/telemetry_payloads.py`，检索有限结果、安全来源与embedding输入前缀

观测模块整体位于 `app/infrastructure/observability/`（仅 Infrastructure，不反向依赖 Application）：

- 观测运行时：`app/infrastructure/observability/setup.py`，实现 `setup_observability`（Tracer/Meter/LoggerProvider 装配、OTLP exporter、采样、四信号状态机、ring buffer span processor、`_resolve_mode` disabled|local|export、诊断句柄与部分失败降级）
- 安全摘要：`app/infrastructure/observability/snapshots.py`，有界指标聚合投影、系统安全子集和最近日志buffer，限额/窗口/截断/深副本与关闭清理
- 自动埋点：`app/infrastructure/observability/instrumentors.py`，实现 FastAPI/HTTPX/SQLite3/SystemMetrics 全局埋点安装、remote parent 提取、有效include_router路由上下文遍历（保留嵌套prefix/method）、span 属性白名单投影、`suppress_client_spans` 抑制作用域与 client span 过滤
- 日志安全出口：`app/infrastructure/observability/logging.py`，实现 fail-closed 事件注册表（`register_safe_event`）、脱敏限长 owned handler 与 stdio stdout 零写入合同
- 采集前脱敏：`app/infrastructure/observability/sanitizer.py`，实现字段白名单、凭据遮盖目录（`configure_secret_catalog`）与嵌套内容键拒绝
- Profile 采样器：`app/infrastructure/observability/profiling.py`，实现 py-spy 子进程窗口采样、折叠栈投影、16 MiB/时窗上限、三振出局退避停用与 Pyroscope 上传

## Interfaces Layer

- HTTP 路由入口：`app/interfaces/http/routes.py`，汇总注册路由
- 健康检查 API：`app/interfaces/http/health.py`，实现 `/health`、`/health/dependencies`
- 文档管理 API：`app/interfaces/http/documents.py`，实现上传、列表、详情、原文查看、标签查询和只读 chunk 查询
- 入库任务 API：`app/interfaces/http/indexing.py`，实现入库触发和任务状态查询
- 检索 API：`app/interfaces/http/retrieval.py`，实现语义检索接口
- 观测自检 API：`app/interfaces/http/observations.py`，实现只读 `GET /observations/otel` 分页快照端点，仅消费组合根注入的 snapshot callable
- 观测外链：`app/interfaces/http/observation_links.py`，沿用N-Agent的服务端四信号链接、Tempo原生Explore/TraceQL/traceId、URL安全与子路径
- HTTP schema：`app/interfaces/http/schemas.py`，定义稳定请求/响应模型和检索输入边界
- HTTP 错误映射：`app/interfaces/http/errors.py`，将应用错误转换为安全、稳定的错误 payload
- MCP 适配：`app/interfaces/mcp/server.py`，使用 FastMCP 暴露 `status` tool；`app/mcp_stdio.py` 组装 stdio MCP 服务，`app/interfaces/mcp/stdio.py` 提供 `python -m` 薄入口；应用入口分别提供 `/mcp` MCP Streamable HTTP 协议站点和 `/mcp/status` 组件状态站点，复用 Application 层健康服务
- FE 静态页面入口：`app/interfaces/http/static/index.html`，定义 Dashboard-first 管理页 app shell、sidebar、topbar 和概览/文档/检索/健康/观测 OTel 容器
- FE 视觉样式：`app/interfaces/http/static/styles.css`，定义管理页 Design Token、布局、表单、按钮、Badge、状态、父子导航和 OTel 面板样式
- FE API 模块：`app/interfaces/http/static/management-api.js`，封装管理页 HTTP API 调用，不直接访问后端基础设施
- FE UI 工具模块：`app/interfaces/http/static/management-ui.js`，封装安全文本渲染、时间格式化、分页、Badge、空/加载/错误状态和标签解析 helper
- FE 导航模块：`app/interfaces/http/static/management-navigation.js`，管理 sidebar 与观测子菜单展开、hash 路由、导航高亮、顶栏标题同步和独立 localStorage 偏好
- FE OTel 页面模块：`app/interfaces/http/static/observations-otel.js`，通过 `init/refresh/deactivate` 管理七面板自检快照、服务端分页、请求竞态和 Grafana 安全外链
- FE 页面入口脚本：`app/interfaces/http/static/app.js`，编排概览、文档、检索、健康与观测页面激活和事件绑定；OTel 具体渲染由独立模块负责

## Docker 与部署

- 镜像构建：`docker/Dockerfile`，使用 Python 3.11 slim 镜像，安装项目并以 Uvicorn 启动服务端口
- Compose 部署：`docker/docker-compose.yml`，定义 `n-kb`、`qdrant`、`ollama`、`ollama-pull-bge-m3` 服务、端口和 volume；该文件被 `.gitignore` 忽略，本地按需保留宿主机路径
- Compose 配置样例：`docker/docker-compose.yml.example`，脱敏后的可提交样例，宿主机挂载目录通过 `N_KB_QDRANT_STORAGE_DIR`、`N_KB_OLLAMA_HOME` 覆盖，默认落在项目内 `locals/` 下；显式映射十二个观测配置，`stop_grace_period` 15s
- 统一观测配置：`docker/observability/compose.yml`，由基础 Compose 顶层 `include` 自动加载；给 n-kb 追加 default 与 external `n-agent-observability` 网络（真实名经 `N_KB_OBS_EXTERNAL_NETWORK` 覆盖，默认 n-telemetry）、可覆盖 collector/pyroscope endpoint、默认关闭的 Profile 开关与 `SYS_PTRACE`；不使用 privileged 或 host PID namespace
- 版本化价格目录：`docker/observability/model-pricing.json`，与 N-Agent 仓相同内容、同 JCS 哈希（出厂空 prices），经 compose 只读挂载到 `/etc/n-kb/model-pricing.json`
- Docker 构建忽略：`.dockerignore`，排除 `.claude`、`.harness`、`.git`、缓存、venv、locals、data
- 本地运行产物：`locals/`、`data/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.egg-info/` 是运行、测试或构建缓存产物

## 测试

- 配置测试：`tests/test_config.py`
- 依赖版本约束测试：`tests/test_dependencies.py`，约束 `pyproject.toml` 中关键依赖的版本范围，避免上游破坏性升级导致镜像构建后容器无法启动
- DDD 边界测试：`tests/test_architecture_boundaries.py`
- Docker Compose 配置测试：`tests/test_docker_compose_config.py`
- Domain 模型测试：`tests/domain/test_models.py`
- 模型成本领域测试：`tests/domain/test_cost.py`，覆盖 AttemptCost 值对象不变量、向量 C 精确 Decimal（0.00002）、显式零价 estimated/0、缺 usage unknown、判定顺序与 TokenValue 拒绝规则
- 定价目录测试：`tests/infrastructure/test_pricing_catalog.py`，覆盖 JCS 哈希/预期哈希核对、重复 price_id 拒绝、精确键/alias 匹配、缺单价 pricing_unavailable、空目录 price_not_found
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
- FE 安全渲染测试：`tests/interfaces/test_management_fe.py`，静态断言 + Node 行为验证（`tests/interfaces/fe_harness/harness.js` DOM/fetch 桩真实执行前端 JS，Node 缺失时行为测试显式 skip）
- 观测生命周期测试：`tests/test_observability_lifecycle.py`，覆盖禁用零装配、lifespan 组装/降级/句柄释放、stdio stdout 合同与多 app 引用幂等
- 观测基础设施测试：`tests/infrastructure/observability/`，覆盖 setup 状态机、instrumentors（含 W3C 传播与畸形 traceparent）、logging fail-closed、sanitizer、profiling 状态机
- 观测语义层测试：`tests/application/test_telemetry_semantics.py`
- 固定契约与请求归属测试：`tests/application/test_telemetry_contract.py`、`test_telemetry_requests.py`，覆盖严格类型、完成快照、结果分类、毫秒/秒换算、十一项详情及线程/并发隔离
- 规范化安全出口与 HTTP 降级测试：`tests/infrastructure/observability/test_normalized_outlets.py`、`test_http_semantics.py`，覆盖真实 SDK 多出口快照一致、伪造归属、秘密隔离、真实传输终态及 SDK 失败日志
- 观测自检 API 测试：`tests/interfaces/test_observations_api.py`，覆盖 disabled/healthy/degraded 合同、分页、并发与序列化哨兵

## Harness 任务文件

- Harness 运行配置：`.harness/harness.json`，启用 Codex 三方审阅与 after-finish Hook，并定义模型和超时等运行值
- Workflow 收尾 Hook：`.harness/hooks/after-finish.sh`，在受支持 Workflow 成功结束后调用现有 `docker/restart.sh`
- 设计 spec：`.harness/specs/active/`
- 人工端到端验收清单：`.harness/specs/verify/`
- 实现 plan：`.harness/plans/active/`
- 架构知识：`.harness/knowledge/02-architecture.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`


## 补全 OTel 观测

- 观测规范与采集矩阵：`.harness/knowledge/07-observability.md`
- 有界摘要测试：`tests/infrastructure/observability/test_snapshots.py`
- Grafana链接测试：`tests/interfaces/test_observation_links.py`
- 共享大盘静态测试：`tests/test_grafana_dashboards.py`，从相邻n-agent检验provisioning、查询、映射与关联字段
- 共享 Collector 测试：`tests/test_collector_semantics.py`，精确字段对照与可选 `NKB_COLLECTOR_SMOKE=1` 隔离 0.137.0 转换，原配置基线比较 N-Agent 输出，限时清理与残留检查
- 共享栈交付：相邻n-agent `docker/observability/grafana/provisioning/dashboards/nkb-overview.json`、`nkb-logs.json`、`nkb-profile.json`及`docker/observability/nkb-metric-name-mapping.md`
