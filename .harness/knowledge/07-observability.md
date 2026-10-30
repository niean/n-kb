<!-- SUMMARY: N-KB 四信号观测覆盖矩阵、安全摘要、N-Agent一致性、Grafana链接与查询验证边界 -->
# 规范观测行为

## 与 N-Agent 一致的实现

同类能力沿用 N-Agent 的 SDK 1.45.1 / instrumentation 0.66b1、SystemMetricsInstrumentor 幂等装配、服务端 Grafana Explore panes、共享数据源 UID、注入快照 callable、安全 DOM 渲染与进程级 py-spy→Pyroscope 模式。不跨仓 import 代码。必要query、有限snippet和安全异常经统一脱敏限长进入Tempo/Loki专用详情；完整文件、向量、凭据和敏感标签不采集，本地最近摘要与Profile不复制正文。

## Trace与Log详情主链路

Application的telemetry_details统一11项详情信封、ContextVar现场与预算，telemetry_payloads构造RAG输入和有限结果；组合根向Infrastructure注入回调。完成日志复用同一Prepared写入所属Span属性、Event、JSON stderr与OTLP Log，未采样仍保留关联日志。每项默认2048 UTF-8字节、整条4倍，记录真实截断元数据；普通应用/库日志与异常恢复可读内容。Collector仅对n-kb显式构造详情JSON，清空已映射日志attributes以阻止未知Loki metadata透传，N-Agent管线不变。

2026-10-31按用户省Token要求优先交付业务详情主链路；通用HTTP正文/流式捕获与隔离Collector转换测试延后，见debt-tracker。

## 采集覆盖矩阵

| 路径/调用点 | Trace | Metric | Log | Profile / 验证 |
|---|---|---|---|---|
| HTTP main.py / instrumentors.py | 白名单业务 SERVER，W3C远程父，降噪健康/静态/自检 | 复用业务指标，不重复HTTP计数 | http_request_completed（方法/模板路由/状态/耗时/终态） | 进程级；test_instrumentors、test_observability_lifecycle |
| MCP HTTP/stdio server.py status | nkb.mcp.status；保留依赖探测CLIENT抑制 | nkb.mcp.requests/duration，operation与outcome | nkb.operation.completed；stdout仅协议 | 进程级；test_mcp_server、test_observability_lifecycle |
| DocumentService.upload_document | nkb.document.upload，拒绝/去重/失败/取消沿原Span终态 | 新增nkb.document.uploads/duration，每次调用一次 | 新增nkb.operation.completed，安全操作/终态/耗时 | 进程级；test_document_service、test_telemetry_semantics |
| IngestionService.index_document | nkb.index.run | jobs/chunks，新增index.duration | 原nkb.index.completed补结构化事实 | 进程级；test_ingestion_service |
| parse/split/embed/write | 每阶段独立Span，失败与取消原样传播 | 新增index.stage.duration，固定stage/outcome | 阶段各一次完成Event/Log，run保留汇总 | 进程级；test_ingestion_service、test_telemetry_semantics |
| OllamaEmbeddingProvider | GenAI embeddings CLIENT，HTTPX实际传输子Span，不重复CLIENT | gen_ai.client.operation.duration/token.usage；token缺失不补零 | http_client_completed | 进程级；test_ollama_provider |
| RetrievalService.search | nkb.retrieval.search，结果数和过滤类型计数 | requests/results/duration，结果直方图强制View含0边界 | nkb.retrieval.completed补结构化事实 | 进程级；test_retrieval_service、test_telemetry_semantics |
| QdrantVectorIndex | 有效父操作内HTTPX CLIENT | 由索引/检索业务指标覆盖，不新增重复依赖指标 | 出站安全HTTP事实 | 进程级；test_instrumentors、test_retrieval_service |
| SQLiteStore | sqlite3 CLIENT，只投影操作/表，禁SQL参数 | 系统/业务指标足够，不新增逐SQL指标 | 不新增逐SQL日志 | 进程级；test_sqlite_store、test_instrumentors |
| 关停 setup.py | 不新增关停业务Span | 共用5秒预算flush；超时丢弃诊断 | owned handlers释放，不递归导出故障 | 先停profiler；test_setup、test_observability_lifecycle |

所有终态在所属Span仍current时记录，success/error/cancelled各计一次，取消不写error.type、不设ERROR、不吞异常。观测失败不改变业务返回。Domain不依赖OTel；Application仅OTel API；SDK、缓冲和自动埋点位于Infrastructure，经composition注入策略。

## 自检与有界摘要

`GET /observations/otel`只读取快照、不flush、不探测后端、Cache-Control=no-store。span最近100条已结束sampled记录，page=1..10/size=10。分页窗口会移动，不代表全量历史。

`metric_snapshot`保留现有15秒reader最近批次的已登记指标，最多32项、每项100点。包含采集时间、窗口、temporality、有限数值与安全维度，截断范围单独标注；overflow独立标识，不推算未知丢弃量。累计值为provider生命周期，delta为采集窗口，gauge为瞬时。摘要失败保留旧值标stale，不阻OTLP；本地截断不影响原导出。系统CPU利用率对去CPU维度的序列取均值，CPU时间和I/O按保留维度累加，设备/接口/地址不进本地摘要。

`logs`容量100、返回最新10条，按收录顺序倒序；retained_count/capacity/returned_count/evicted_count明确表示本地窗口覆盖。SafeLogBridge的Prepared投影供JSON、span event、OTLP和本地缓冲复用，未登记事件只诊断码。业务上下文由composition注入current_operation_context，属性只用精确枚举/非负有限数值，不信任任意extra。local模式虽无LoggerProvider仍收录本地安全事件。最后引用释放后清空并关闭全部缓冲。

## Grafana与交付边界

保留旧base_url/traces_base_url，新增metrics/logs/profile三个可选外链，单数profile命名与N-Agent一致。base_url可派生固定UID：nkb-overview、nkb-logs、nkb-profile及Explore；显式入口优先，未知Trace入口不拼原生查询。URL限制HTTP(S)、host、无userinfo/控制字符/秘密查询键，保留非冲突参数及反向代理子路径。

Explore使用nagent-tempo原生panes和TraceQL resource.service.name/service.instance.id。仅有效非零32位Trace ID生成traceId深链；未采样的有效ID不保证后端存在数据。Profile按实际上传service_name查询，为服务级，不伪造实例或逐Trace关联。

大盘配置位于相邻N-Agent仓库`docker/observability/grafana/provisioning/dashboards/nkb-*.json`。指标映射及证据见该仓库`docker/observability/nkb-metric-name-mapping.md`。自动测试仅证明静态配置/隔离SDK合同；真实后端四信号连通与关联按本任务verify清单人工验收，未运行不得宣称已接通。

## FastAPI 路由树与远端 Trace

入站SERVER白名单必须遍历有效路由上下文，而非假定app.routes平铺。FastAPI>=0.137的include_router可能只有树节点；优先使用公开fastapi.routing.iter_route_contexts保留完整prefix和methods，较早版本兼容effective_route_contexts，老版本直接使用平铺route。未知路径、错误方法和既有噪声排除仍按原规则处理，不遍历Mount内部应用。

上游CLIENT与N-KB内部Span的时间吻合但Trace不同，先检查N-KB是否存在SERVER Span。白名单漏业务会过滤SERVER，随后Application操作变成新根；这并不证明上游没发traceparent。回归覆盖真实APIRouter嵌套、有/无采样、tracestate和直接父Span，不通过手工赋Trace ID修补。运行容器和宿主FastAPI版本不同须各自验证。

2026-10-30 实际容器验收：FastAPI0.162的raw ASGI/SDK探测在修前为0SERVER，修后有采样与未采样均复用远端上下文。重建n-kb后，从n-agent容器通过其生产HTTPX埋点发出只读检索，Tempo的9240565fe85932e803071738e6e44b61同时包含n-agent与n-kb；N-Agent CLIENT a0dfd91c96efdf90等于N-KB SERVER 98cd0d00eab294bf的parent_span_id，后续检索/embedding/Qdrant均在同Trace。历史分裂Trace不回写合并。

HTTP采集现沿N-Agent实际读写tap实现于http_capture.py：ASGI receive/send与HTTPX同步/异步transport流，不预读或改变payload；保存显式HTTP owner，在SDK结束前发布http_request/http_response到属性、唯一完成Event及Log。ASGI最终send成功后才结束Span，早关/错误标不完整。检索JSON收脱敏输入/有限结果；Ollama/Qdrant向量和上传文件仅摘要/元信息，未批准管理接口不收正文。原始前缀最多4倍内容预算，gzip/deflate有界解压，超限/不完整正文保守省略；最近摘要仍不含正文。
