<!-- SUMMARY: N-KB 统一观测语义：对象、操作、属性、结果、完成事实、身份、十三项指标、内容出口及共享查询验证边界 -->
# 规范观测行为

## 共性规范与整体模型

观测先定义观察谁、执行什么、发生什么、怎样结束及如何关联，再映射 Trace、Log/Event、Metric 与 Profile。参照 N-Agent 的统一语义原则，N-KB 保持独立服务身份 service.name=n-kb，不跨仓 import，不把知识库操作伪装成 AgentRun。

Trace 表示执行过程和父子关系；完成 Log/Event 表示同一次结束事实；Metric 测量明确范围内的增量或分布。Profile 使用进程级 py-spy→Pyroscope，尚未接入 OTel Profiles，也不声称逐 Trace 关联。OTel SDK 1.45.1 / instrumentation 0.66b1 与 GenAI 草案 semantic-conventions-genai main @ 2026-10-24 (Development) 沿用锁定版本，升级需另行核对。

记录结构中的 Trace/Span ID、父级、时间、状态、Event、Log 严重度/正文和 Metric exemplar 沿用 OTel；Resource 的 service.name/version/instance.id 表示服务身份，InstrumentationScope 表示埋点来源，不能用业务对象 ID 冒充这些身份。

## 对象与操作

固定对象为 document、index、retrieval、model、mcp、request、http、observation、metric、content。自定义 Span 名为 object.action，属性 nkb.<object>.operation 只记录 action；唯一 ID、query、标题与正文不进入操作名称。

| 对象 | 操作与边界 | 完成事件 |
|---|---|---|
| document | document.upload；一次上传，包括正常去重、拒绝、失败 | nkb.document.completed |
| index | index.run；一次实际入库调用 | nkb.index.completed |
| index | index.parse/split/embed/write；四个真实阶段，action 与 run 区分 | nkb.index.completed |
| retrieval | retrieval.search；一次真实检索，空结果也是成功 | nkb.retrieval.completed |
| model | embeddings；一次实际 Ollama 请求，CLIENT Span 名可附配置模型 | nkb.model.completed |
| mcp | mcp.status；一次 status tool 调用 | nkb.mcp.completed |
| request | request.handle；HTTP 业务入口，复用 SERVER，不另建 Span | 复用 nkb.http.completed |
| http | 实际 HTTP SERVER/CLIENT，沿用自动埋点 Span 名与标准方法 | nkb.http.completed |
| observation / metric / content | 观测诊断、基数归并及安全内容，不补造业务 Span | 按真实记录归属 |

模型仅写 gen_ai.operation.name=embeddings，不另写 nkb.model.operation；HTTP 不写 nkb.http.operation。MCP status 不是 N-Agent execute_tool，retrieval.search 是 N-KB 自定义检索操作，不凭名称伪装标准 Agent 或外部平台类型。SQLite 使用 DB CLIENT 语义，不发布 HTTP 完成事件。空 embedding 输入不请求、不创建 model Span。未知操作记录安全 unknown_operation 诊断，隔离自身上下文，不改写父级事实。

## 属性与类型

项目字段使用 nkb.<object>.<attribute>，对象与属性小写 snake_case；已有标准字段保留原名。契约精确列举字段，不按 nkb/gen_ai 前缀放行。通用事实入口 project_execution_attributes 排除所有内容与内容元数据；类型通过不代表内容可导出。

| 归属 | 字段 | 类型与采集边界 |
|---|---|---|
| 各操作对象 | nkb.<object>.outcome / summary / duration_ms | 固定结果枚举、登记的固定完成摘要、有限非负毫秒；结束时一次冻结 |
| 自定义操作对象 | nkb.document/index/retrieval/mcp/request.operation | 对应 upload、run/四阶段、search、status、handle |
| document | nkb.document.id | 上传成功后真实文档 ID；非空、最长 128 字符、受限字符集合 |
| index | nkb.index.document_id / job_id / stage / chunk_count | 真实文档与任务 ID；parse/split/embed/write；非负 int |
| retrieval | nkb.retrieval.result_count / filter_tags | 实际结果数、过滤标签数；非负 int，不记录标签值 |
| retrieval | nkb.retrieval.filter_source_kind / filter_document_status / filter_min_score | 非负 int 存在计数，固定 0/1，不记录过滤值 |
| model | nkb.model.usage_known | bool；输入 Token 是否已知，不以未知冒充零 |
| 模型标准 | gen_ai.operation.name / provider.name / request.model / usage.input_tokens / token.type | embeddings / ollama / 受控配置模型 / 已知非负 int / input；token.type 仅指定 Token instrument 维度 |
| request | nkb.request.channel / id | api/mcp_http/mcp_stdio/unknown；ID 无真实受信来源时省略，此次未新增请求 ID 协议 |
| http | nkb.http.direction / caller | server/client；受控调用位置枚举 ollama.embed、qdrant.request、health.check、http.request、unknown |
| HTTP 标准 | http.request.method / response.status_code / route、server.address / port、url.template | 实际方法、实际状态、模板及安全对端信息；无响应不补码，拒绝 bool 状态码与非法端口 |
| DB/兼容标准 | db.system / system.name / operation / collection.name 等锁定自动埋点字段 | 精确白名单；SQL 只投影表名，不记录 SQL/参数 |
| 错误 | error.type | unsupported_file_type/file_too_large/embedding_failed/vector_index_failed/sqlite_failed/timeout/other；取消不写技术错误 |
| observation / metric | nkb.observation.error / nkb.metric.overflow | 固定安全诊断枚举 / bool，非正文和业务 ID |
| content | nkb.content.<field> 及四项后缀 | 见内容出口，只能经过脱敏有界详情通道 |

计数拒绝 bool、负数及隐式转换；耗时拒绝 NaN/Infinity/溢出数；模型同时校验配置来源、长度与字符集合。非法值省略，业务继续。HTTP 自动埋点锁定版本的新旧标准属性允许集合见 instrumentors.ALLOWED_AUTO_ATTRIBUTES，不能由其存在推导项目字段命名。

## 事件、状态与结果

统一完成结果为 success、failure、denied、cancelled、timed_out、skipped、deferred、unknown。仅在有真实事实时使用：

| 结果 | 含义与状态 |
|---|---|
| success | 正常完成，包括真实空结果和正常去重 |
| failure | 异常或明确失败返回；Span ERROR |
| denied | 实际准入拒绝；不自动设技术 ERROR |
| cancelled | CancelledError/GeneratorExit 等实际取消；无 error.type，不自动 ERROR |
| timed_out | TimeoutError 或受信 httpx 超时异常链；Span ERROR，不按文本猜测 |
| skipped | 规则确认没有执行；当前无新增业务分支，仅保留契约 |
| deferred | 本次处理已结束但等待后续条件；当前无新增业务分支 |
| unknown | 无法识别，不伪装成功 |

DocumentStatus、IndexJobStatus 与各阶段结果保持各自业务含义，不能用观测结果替换。运行中不预写 success，summary 为固定操作完成摘要，不含文档或异常正文。

生命周期唯一冻结 CompletionSnapshot，所属 Span 仍 current 时测量指标、写入属性和一个完成 Event，再由受信回调发布一个完成 Log。冻结耗时的属性使用毫秒、duration instrument 使用秒。各阶段与 run 各有自己的完成事实，不是重复事件。日志桥不补计数，也不能因为普通日志正文等于登记完成模板就补造完成 Event。未采样或 Span 创建失败仍尝试安全完成日志；只在有效 SpanContext 时附 Trace/Span ID。

HTTP 完整 2xx/3xx 为 success，4xx/5xx 或传输异常为 failure；明确准入拒绝且实际 4xx 可为 denied；实际取消/超时优先分类，无明确原因的未完成传输为 unknown。SERVER 4xx 不自动 ERROR，CLIENT 4xx 与两端 5xx 沿用 HTTP 错误规则。尚未成功发送 response.start 不记录状态码。HTTP 结果与业务 request 结果分别计算：业务超时可返回实际 502，此时 request=timed_out、http=failure。没有业务结果时省略 request.outcome。

## 身份与跨信号关联

同步调用继承直接父级，N-Agent 到 N-KB 使用 W3C traceparent/tracestate 关联，不读取或伪造 nagent.agent.*。业务 document/job/request 身份各自归属；ID、正文、来源和标签值不进入普通 Metric/Loki 索引标签。

操作 ContextVar 只继承受信身份，子操作不继承父级结果、摘要、耗时或详情；退出恢复，并发结果隔离。HTTP 请求使用独立 request owner，由线程池中的同步路由回传真实结果给 ASGI 完成记录，避免 ContextVar 替换不能反向传播；嵌套和并发请求不串号。channel 来自入口代码，不信任请求头、baggage 或任意 extra。MCP stdio 在每次 tool 调用时绑定 mcp_stdio，而非仅构造 server 时设置。stdio 不造 request/HTTP Span。

当前入库同步执行，不新增持久化 trace 字段或后台恢复协议。Trace/Span ID 串起信号，部分测量可带 exemplar，未采样有效 ID 不保证后端有 Trace。Profile 仍是服务进程级。

## 指标

十三项 instrument 保留原名称、类型、单位、触发含义和直方图边界；只迁移项目维度。边界及精确维度以 telemetry_contract.INSTRUMENTS 为唯一实现定义，测试逐项固定期望，禁止从注册表自身生成期望。

| instrument | 类型 / 单位 | 一次测量及允许维度 |
|---|---|---|
| nkb.document.uploads | Counter / 1 | 每次上传结束；nkb.document.outcome |
| nkb.document.upload.duration | Histogram / s | 同一上传耗时；nkb.document.outcome |
| nkb.index.jobs | Counter / 1 | 每次入库结束；nkb.index.outcome |
| nkb.index.duration | Histogram / s | 同一入库耗时；nkb.index.outcome |
| nkb.index.chunks | Histogram / {chunk} | 成功入库实际 chunk 数，含真实零；无业务维度 |
| nkb.index.stage.duration | Histogram / s | 每次真实阶段结束；nkb.index.stage、nkb.index.outcome |
| nkb.retrieval.requests | Counter / 1 | 每次检索结束；nkb.retrieval.outcome |
| nkb.retrieval.duration | Histogram / s | 同一检索耗时；nkb.retrieval.outcome |
| nkb.retrieval.results | Histogram / {result} | 成功检索实际结果数，含零；无业务维度；桶首边界 0 |
| nkb.mcp.requests | Counter / 1 | 每次 status 结束；nkb.mcp.operation、nkb.mcp.outcome |
| nkb.mcp.duration | Histogram / s | 同一 MCP 操作耗时；同上 |
| gen_ai.client.operation.duration | Histogram / s | 每次实际 embedding 尝试；gen_ai.operation.name/request.model、error.type |
| gen_ai.client.token.usage | Histogram / {token} | 已知输入 Token，真实零可记、未知省略；gen_ai.operation.name/request.model/token.type |

Prometheus 将维度点转换为下划线，如 nkb_index_outcome；名称与单位翻译见相邻 n-agent 的 docker/observability/nkb-metric-name-mapping.md。错误率为 failure+timed_out 除以 success+failure+timed_out，取消与准入拒绝单独观察，不塞入该分母。零分母或缺序列显示无数据，不以 vector(0) 伪造。跨实例先 rate 再 sum，p95 保留 le；Token 使用 histogram sum，空命中率使用 le=0 桶与 count。

## 内容与出口

固定十一项：retrieval_input、retrieval_output、document_input、index_input、index_result、embedding_input、embedding_result、http_request、http_response、error、log_fields。各项为 nkb.content.<field> 正文及 _original_bytes、_truncated、_incomplete、_observed_bytes 四项元数据。Collector 整理为 details.<field>.{content,original_bytes,truncated,incomplete,observed_bytes}。

telemetry_details 统一先脱敏再限长，默认每项 1024000 UTF-8 字节（配置范围 1–1024000）、整条四倍预算；未知完整长度省略 original_bytes，observed_bytes 只描述已观察长度。telemetry_payloads 保留必要 query、安全来源与有限 snippet。完整文件、原文、向量、凭据和敏感标签禁采；媒体省略。HTTP byte tap 沿实际 ASGI/HTTPX 读写捕获，不预读或重复消费，gzip/deflate 有界解压，超限或不完整正文保守省略，中断标 incomplete。

Span/Log/Event 复用同一安全投影及元数据。通过注入的 completion_scope 批量发布准备好的属性，避免包装器逐字段再次处理而丢失完整长度。普通应用/库日志仍可有脱敏有界正文和异常；任意 extra 只能经 log_fields 详情通道，不能建立身份或完成记录。受信归属采用私有 side table，不认 LogRecord extra 的伪造标记。

Metric、Profile、自检 Span/log ring 不带正文、详情或异常文本，只保留允许事实与固定摘要。各出口精确放行，不按前缀信任。stderr 与 Loki 的 context.execution 保留规范属性；event、severity、message、trace_id/span_id、details 等结构字段保留自身名称。Collector 只修改 n-kb 分支，完成显式投影后清空 N-KB 日志 attributes，防止未知 metadata 透传；N-Agent 输出基线等价。

## 实现与故障隔离

| 层次 / 文件 | 职责 |
|---|---|
| Domain | 业务对象、端口、状态，不依赖 OTel |
| Application telemetry_contract.py | 标准库固定对象、操作、事件、类型、结果、指标与内容字段 |
| Application telemetry_semantics.py | OTel API 生命周期、冻结完成快照、唯一测量与发布 |
| Application telemetry_requests.py | 请求 owner、线程池结果回传与入口身份 |
| Application telemetry_details.py / telemetry_payloads.py | 统一详情信封、脱敏预算与有限 RAG 内容 |
| composition.py | 注入观察器、精确投影、受信完成出口及清理回调 |
| Infrastructure observability | SDK、自动埋点、日志桥、缓冲、导出与 Profile，禁止反向 import Application |
| Collector / Grafana | 同一字段协议的整理、存储与查询 |

观测创建、属性写入、异常格式化、详情回调和导出故障均隔离，不改变业务返回、原异常、取消或数据流消费。初始化与最后引用释放幂等，关闭时恢复回调与清理缓冲；沿用有界队列、基数、重试和共用五秒 flush/shutdown 预算。禁用不装 SDK。Trace 使用 ParentBased 比例采样；Log/Metric 不随 Trace 未采样丢弃。Profile 先停止再释放观测句柄。

FastAPI SERVER 白名单沿有效路由树遍历，保留 include_router 嵌套 prefix/method，兼容公开 iter_route_contexts、effective_route_contexts 与早期平铺版本；过滤健康、静态、自检噪声，HTTPX/SQLite 仅在有效父级内创建 CLIENT。SQLite cursor 拦截与 HTTP 传输分开，不因同为 CLIENT 就认作 HTTP。没有 SERVER 时应先检查白名单与版本，不能直接认定上游没有 traceparent。

## 自检、外链与共享展示

GET /observations/otel 只读快照，不 flush 或探测后端，Cache-Control=no-store。Span 最近 100 条结束 sampled 记录，每页 10 条；日志容量 100、返回最新 10 条；重启及最后释放清空，不是历史全量。

metric_snapshot 保留 15 秒 reader 最近批次，最多 32 项、每项 100 点；窗口、temporality、截断和 overflow 单独标注，失败保留旧值标 stale，不阻 OTLP。不暴露设备、接口和地址；CPU 利用率去 CPU 维度后均值，CPU 时间及 I/O 按允许维度累加。span/log ring 无正文。

服务端生成四信号外链，沿用 N-Agent Tempo Explore panes 和共享数据源 UID，固定大盘 UID nkb-overview/nkb-logs/nkb-profile；保留反向代理子路径与非冲突参数。仅有效非零 32 位 Trace ID 拼深链，URL 限 HTTP(S)、host、无 userinfo/控制字符/秘密查询键。当前共享大盘沿用既有 service 单选及 All regex，不新增 instance 筛选；Profile 固定 service_name=n-kb，不声称实例或逐 Trace 关联。共享布局契约仅覆盖 overview 前 11 个公共面板，N-Agent 后续领域面板不镜像。

## 验证与部署边界

2026-11-04 规范化任务最终自动验证：NKB_OTEL_SDK_SMOKE=1 NKB_COLLECTOR_SMOKE=1 python -m pytest -v -rs，820 passed、1 skipped。跳过项是单独启用的真实镜像构建测试；真实 SDK 本地往返、完成快照跨出口一致、父级传播、上下文隔离、失败/拒绝/超时/取消、秘密分片、严格字段与故障注入均有自动覆盖。两仓 Compose config --quiet 均成功。

隔离 Collector 0.137.0 使用真实 processors 与顺序，仅替换 IO；固定完成日志和 library_log 覆盖执行事实、全部十一项详情/元数据、Trace/Span、清空未知属性，N-Agent 与 git HEAD 基线输出等价。Docker/git 命令及轮询均限时，启动失败也清理随机临时容器并检查残留，不连接共享后端。

以上不是实际 Tempo/Loki/Prometheus/Pyroscope 存储或 Grafana UI 验收。人工清单见 .harness/specs/verify/verify-261104-normalize-observability.md。应用 after-finish Hook 已重建 N-KB 并通过 /health（status=ok）。共享 Collector/Grafana 本次未重载，Hook 只重建 N-KB；共享配置需按部署流程生效，重载前旧 Collector 仍不能展示新字段。

不删除、改写、回灌历史观测，不双写旧字段。新大盘默认新契约，旧时间段可能不完整，不隐式合并历史标签。历史路由树问题曾于 2026-10-30 以容器内 SDK/ASGI 和跨服务 Trace 验证修复，那是当时证据，不保证旧观测现在仍保留；本次未回写历史 Trace。
