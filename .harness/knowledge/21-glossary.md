<!-- SUMMARY: N-KB 的核心术语，包括文档、来源、标签、分类、chunk、embedding、向量索引、入库任务、检索结果和 Langfuse 观测；模型成本观测术语（AttemptCost/计价身份/content_version@sha256/JCS/成组校验） -->
# 术语表

- N-KB：独立知识库与 RAG 服务，为 N-Agent 和其它调用方提供文档管理、入库索引和语义检索能力。
- N-Agent：N-KB 的调用方之一，通过 HTTP API 获取检索结果，不直接依赖 N-KB 内部代码。
- Document：知识库文档聚合根，代表一个被管理和可索引的知识条目。
- DocumentSource：文档来源，记录上传、本地文件、网页、Git、API 等来源类型和来源标识。
- DocumentContent：文档原文，保存上传或导入后的可索引文本内容。
- Original File：上传或导入时保存的原始文件，用于追溯、重建索引和管理展示。
- Tag：统一 key-value 标签，格式为 `key=value`，用于分类、过滤和组织文档。
- Category：分类标签，是固定 key 的标签，格式为 `category=<value>`，不作为独立分类树字段。
- Chunk：从文档原文切分出的文本片段，是 embedding 和向量检索的基本单位。
- Embedding：文本向量表示，由 Ollama+BGE-M3 根据文档 chunk 或查询文本生成。
- Vector Index：向量索引端口，当前 Infrastructure 由 Qdrant 实现。
- Qdrant：向量数据库，用于保存 chunk 向量和检索 payload。
- Ollama：本地模型服务，用于运行 BGE-M3 embedding 模型。
- BGE-M3：Embedding 模型，用于将文档 chunk 和 query 转换为向量。
- LlamaIndex：RAG 编排代码框架，用于文档解析、切分和索引/检索辅助，不是独立服务。
- IndexJob：入库或重建索引任务，记录文档索引流程的状态、阶段和错误摘要。
- RetrievalQuery：检索请求领域对象，包含 query、标签过滤、top_k 和 min_score。
- RetrievalResult：检索结果领域对象，包含文档、chunk、分数、片段、来源和标签。
- Management FE：N-KB 提供的简单前端，用于上传文档、管理原文件、查看标签和验证检索。
- OTel：OpenTelemetry 可观测标准，N-KB 内部观测遵循其 Trace/Metric/Log/Profile 四信号规范，依赖固定 1.45.1 / instrumentation 0.66b1。
- OTLP：OTel 导出协议，N-KB 经 OTLP/gRPC 将 Trace/Metric/Log 导出到 N-Agent 共享观测栈的 Collector。
- Semconv：语义约定，N-KB 观测属性命名（如 `nkb.outcome`、`error.type`、`genai.*`）遵循的约定版本，版本号由 `telemetry_semantics.SEMCONV_VERSION` 声明。
- Trace 串联：N-Agent 调用 N-KB 时经 W3C tracecontext（traceparent/tracestate）传播，使两服务 span 在 Tempo 中呈同一 trace 的直接父子关系，并在共享 Langfuse 项目内按相同 Trace ID 合并为完整父子树。
- Ring Buffer Span：观测运行时内存保留的最近 100 条已结束 sampled span 摘要（八个固定字段），供 `GET /observations/otel` 自检分页展示。
- Delivery：自检快照中单信号出口的最近投递状态，取值 disabled/unknown/succeeded/failed；仅表示本进程最近一次导出确认，不代表后端已持久化。
- Profile：进程 CPU 采样画像，由 py-spy 子进程按窗口采样生成折叠栈，经安全投影上传 Pyroscope；需要 SYS_PTRACE 能力（仅 profiling override）。
- Langfuse：LLM 观测平台（共享栈部署于 n-agent 仓，版本锁定 4.76.0）。N-KB 应用零 Langfuse SDK，Trace 经 N-Agent 共享 Collector fork 的 traces/langfuse-kb 管线以 nkb 专用投影写入单一共享项目 n-agent-local-project（与 N-Agent 同项目、同组项目 Basic 凭据，无独立 N-KB 项目）；跨服务 Trace 关联与映射类型、安全边界的权威定义见 .harness/knowledge/07-observability.md "Langfuse 映射契约"。
- n-agent-local-project：Langfuse 共享项目，同时承载 N-Agent 与 N-KB 两个服务的 span；凭据经宿主 docker/langfuse/.env（0600）注入，不轮换。

## 模型成本观测术语

- AttemptCost：单次 embedding 尝试的成本事实值对象（`app/domain/cost.py`，与 N-Agent 同语义独立实现，不跨仓导入），每次实际 Ollama 请求恰计算一次；estimated 携带 USD 总额、input 分项、pricing_version/price_id，unknown 只携带受控 reason、无金额。Span/完成事实/Counter 消费同一结果。
- TokenValue：用量桶三态值对象；N-KB 的 input 为已知桶，output/cache_read/cache_write 由协议适配器声明 not_applicable，不输出虚构零 Token；缺计量即使显式零价也 unknown。
- 计价身份（billing identity）：`(billing_provider, billing_model)`，N-KB 固定取配置原始模型名（settings.embedding_model）与 ollama/other 枚举精确查价；观测模型名白名单归一在后，不丢失原始计价身份。
- content_version@sha256：价格目录版本标识 = 目录内 `content_version` 字段 + 实际内容哈希（JCS 规范化 SHA-256，与启动期预期哈希 `N_KB_PRICING_CATALOG_EXPECTED_SHA256` 核对）；与 N-Agent 部署相同内容的目录文件。
- JCS（RFC 8785）：JSON Canonicalization Scheme，价格目录哈希对 JCS 规范化后的 UTF-8 字节计算；实现位于 `app/infrastructure/embedding/pricing_catalog.py`，禁止用 sort_keys 的 json.dumps 冒充。
- 成组校验：Langfuse KB 管线对成本字段的成组守卫，不部分成功——cost_details 仅在 estimated 且币种/分项/版本/身份全部合法时投影（单 input 分项 + total）；非法组 fail-closed 整组拒绝并保留审计 metadata，独立合法 Token 保留。
- unknown 不推价：cost_status != estimated 时 Langfuse 不投影 observation.model.name，阻断平台按模型名自动推价；原始模型键（gen_ai.request.model）保留在 metadata 供分析。
