# PROJECT.md -- N-KB

N-KB 是独立的知识库与 RAG 服务，为 N-Agent 和其它调用方提供文档管理、知识入库、向量检索和检索结果 API。

---

# Harness 框架适配

本节为 Harness 框架提供项目级配置，框架文件通过 `.harness/PROJECT.md` 直接引用。

## 知识库目录

首次加载时需建立 SUMMARY 索引的目录：
- `.harness/knowledge/`
- `.harness/prd/`（除 .harness/prd/03-prd-specs.md）
- `.harness/lessons/`

## 任务类型加载矩阵

首次加载时，根据任务类型选择性读取知识库文件（所有文件首行 SUMMARY 始终必读）：

| 任务类型 | 必读（完整读取） | 按需读取 |
|---------|----------------|---------|
| 功能需求 | .harness/knowledge/01-overview.md, .harness/knowledge/02-architecture.md, .harness/knowledge/22-file-map.md, .harness/prd/01-prd-sense.md, .harness/prd/02-prd-baseline.md | .harness/knowledge/03-conventions.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| 功能精调 | .harness/knowledge/01-overview.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/03-conventions.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| Bug修复 | .harness/knowledge/01-overview.md, .harness/knowledge/03-conventions.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| 治理/扫描 | .harness/knowledge/01-overview.md, .harness/knowledge/03-conventions.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/05-key-patterns.md |
| 文档维护 | .harness/knowledge/01-overview.md, .harness/knowledge/22-file-map.md | 读取目标文件引用链上的 knowledge/ 和 prd/ 文件 |

## 知识回填文件映射

知识回填的回填目标：
- 架构变化 -> .harness/knowledge/02-architecture.md
- 新术语 -> .harness/knowledge/21-glossary.md
- 数据结构/存储变化 -> .harness/knowledge/04-data-boundaries.md
- 新源文件 -> .harness/knowledge/22-file-map.md
- 新跨文件模式 -> .harness/knowledge/05-key-patterns.md
- 产品方向调整 -> 提示用户，人工更新 .harness/prd/01-prd-sense.md

## 教训库加载路径

本项目教训库分布在两个位置：
- `.harness/framework/lessons/general.md`（Harness 通用教训）
- `.harness/lessons/project.md`（项目教训）

## 构建与测试

### 构建
```bash
python -m pytest -v
```

### 单元测试
单元测试执行策略：
- 用户明确要求时：必须执行
- 新增 Domain、Application、Infrastructure、Interfaces 能力时必须执行相关测试
- 涉及 DDD 边界、配置、Docker Compose、存储 schema、RAG 检索链路时必须执行对应测试
- 其它仅文档变更场景可跳过运行测试，但需检查模板占位符和引用路径

```bash
python -m pytest -v
```

## 扫描维度

代码扫描使用的维度及规则来源。下表路径均相对于 `.harness/knowledge/` 目录：

| # | 维度 | 规则来源 |
|---|------|---------|
| 1 | DDD 分层边界 | 02-architecture.md, 03-conventions.md |
| 2 | 数据与存储边界 | 04-data-boundaries.md |
| 3 | RAG 外部依赖边界 | 02-architecture.md, 04-data-boundaries.md |
| 4 | 配置与密钥安全 | 03-conventions.md |
| 5 | 文件管理与上传安全 | 03-conventions.md, 04-data-boundaries.md |
| 6 | Docker Compose 部署一致性 | 01-overview.md, 03-conventions.md |

可选（涉及文件删除时）：

| # | 维度 | 规则来源 |
|---|------|---------|
| 1 | 文件职责映射一致性 | 22-file-map.md |

## 项目知识索引

| 文件 | 何时查阅 |
|------|---------|
| .harness/prd/01-prd-sense.md | 功能迭代前，确认产品定位和判断准则 |
| .harness/knowledge/01-overview.md | 任务开始时，了解项目概览（技术栈/入口/核心流程） |
| .harness/knowledge/02-architecture.md | 涉及 DDD 分层、模块边界、RAG 服务边界时 |
| .harness/knowledge/03-conventions.md | 编码、测试、配置、安全、Docker Compose 约束不清楚时 |
| .harness/knowledge/04-data-boundaries.md | 涉及文档、标签、chunk、embedding、SQLite、Qdrant 数据结构时 |
| .harness/knowledge/05-key-patterns.md | 实现上传入库、检索、依赖注入、外部适配器等跨文件流程时 |
| .harness/knowledge/21-glossary.md | 对知识库、RAG、标签、来源、索引等术语不清楚时 |
| .harness/knowledge/22-file-map.md | 确定功能对应源文件时 |
| .harness/prd/02-prd-baseline.md | 确认功能需求与产品约束时 |
| .harness/lessons/project.md | 用户指令或当前根因与 SUMMARY 高度相关时按需读取 |

---

# 项目规范

## 代码生成

以下各节（代码生成、架构边界、质量守护、安全规范）为快速参考摘要，权威定义见 .harness/knowledge/03-conventions.md。

- 语言：Python 3.11+。
- 应用包：业务代码放在 `app/`，测试放在 `tests/`。
- 框架：HTTP 服务使用 FastAPI + Uvicorn，工作流编排可使用 LangGraph，RAG 编排使用 LlamaIndex。
- 存储：SQLite 保存文档元数据、原文、标签、入库任务和索引状态；Qdrant 保存向量和 chunk payload。
- 部署：Docker Compose 是默认本地部署方式，不使用 host network。
- 配置：运行配置通过 `.env` 或环境变量读取，前缀为 `N_KB_`。
- 文档：不主动创建 README；`.harness/prd/` 为 AI-READONLY，未经人工确认不得修改。

## 架构边界

- 严格遵循 DDD 分层，依赖方向为 Interfaces -> Application -> Domain，Infrastructure 实现 Domain 端口并由应用入口注入。
- Domain 层定义 Document、KnowledgeSource、Tag、Chunk、Embedding、RetrievalResult 等核心模型和端口协议。
- Application 层编排上传、入库、索引、检索和管理用例；LangGraph 若使用，只能位于 Application 层。
- Infrastructure 层实现 SQLite、Qdrant、Ollama Embedding、LlamaIndex 文档解析/索引适配和本地文件存储。
- Interfaces 层实现 FastAPI API 和简单 FE，只做协议转换、HTTP 响应、静态资源交付，不直接访问 SQLite/Qdrant/Ollama。
- `app/main.py` 是唯一负责组装 Infrastructure 具体实现的位置。

## 质量守护

- 新增 Domain、Application、Infrastructure、Interfaces 能力时必须补充对应测试。
- 涉及 DDD 边界时必须增加或运行架构边界测试。
- 涉及 Docker Compose 变更时必须运行 Docker Compose 配置校验。
- 涉及 RAG 链路时测试至少覆盖文档入库、chunk 生成、embedding 调用边界、向量写入边界和检索结果转换。
- 错误、警告、模板占位符残留均视为验收不通过。

## 安全规范

- Provider API Key、Embedding 服务凭据和任何密钥只通过环境变量注入，不写入镜像、测试、日志或文档。
- 上传文件必须限制文件类型、大小和存储目录，拒绝路径穿越和软链接逃逸。
- 原文、标签和来源属于用户知识数据，日志中不得输出完整原文或敏感标签值。
- FE 渲染文档标题、来源、标签和检索片段时必须使用安全文本渲染，不拼接未转义 HTML。
- Docker Compose 挂载目录必须明确，只挂载 n-kb 所需 data/locals/workspace，不挂载敏感大目录。

---

# 项目附录

## 仓库结构

```
AGENTS.md              -- AI 入口（纯路由）
CLAUDE.md              -- Claude Code 入口
.harness/
  PROJECT.md           -- 项目规范入口（本文件）
  framework/           -- 通用能力（详见 FRAMEWORK.md "Framework 目录结构"）
  knowledge/           -- AI 知识库（01~05 认知约束类, 21~22 工具索引类）
  prd/                 -- 产品文档（AI只读：01-prd-sense、02-prd-baseline、03-prd-specs）
  lessons/
    project.md         -- 项目教训（AI自主维护）
  specs/               -- 设计文档
    active/
    completed/
  plans/               -- 实现计划
    active/
    completed/
    debt-tracker.md    -- 技术债追踪
app/
  main.py              -- FastAPI 应用入口与依赖组装
  config.py            -- N_KB_ 配置模型
  domain/              -- 领域模型与端口
  application/         -- 用例服务与 LangGraph 工作流
  infrastructure/      -- SQLite、Qdrant、Ollama、LlamaIndex、本地文件实现
  interfaces/          -- HTTP API 与 FE 静态资源
  interfaces/http/static/ -- 简单 FE 页面资源
tests/                 -- pytest 测试
Dockerfile             -- 容器镜像构建
docker-compose.yml.example -- Docker Compose 示例
pyproject.toml         -- Python 依赖与 pytest 配置
```

## 知识层级关系

```
Layer 0   AGENTS.md -> FRAMEWORK.md（通用规范+注册表） + PROJECT.md（项目配置+规则摘要）
Layer 1   framework/agents/（5个角色: Orchestrator/Designer/Planner/Coder/Reviewer）
Layer 1.5 framework/workflows/（迭代功能/修复Bug/迭代文档 + harness-ops/治理类）
Layer 2   framework/skills/（harness/ 核心Skill + harness-ops/ 运维Skill + superpowers/ 方法论）
Layer 3   framework/skills/harness/subskills/（扫描模板）
数据层    knowledge/（权威知识） + prd/（产品文档，AI只读） + guides/（方法论） + lessons/（教训）
辅助层    specs/（设计文档） + plans/（执行计划+技术债）
```

引用方向：Layer 0 -> Layer 1/1.5 -> Layer 2 -> Layer 3 -> 数据层。PROJECT.md 摘要引用 knowledge/03-conventions.md（权威源）。
