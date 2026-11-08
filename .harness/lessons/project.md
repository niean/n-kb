<!-- SUMMARY: N-KB依赖版本、SQLite自动埋点、入口分层、同名包解析、FastAPI路由树、完成快照准备属性及Langfuse配置生效教训 -->
# 项目教训

AI 自主维护，人工可通过提示或建议触发新增/修正。
项目教训绑定{{项目名称}}，不随 Harness 模板提取。

---

### P001: 关键 Python 依赖必须 pin 上界，否则上游破坏性升级会让容器无法启动
- 现象：`docker/restart.sh` 重建镜像后容器立刻退出，`curl /health` 失败，`docker compose ps n-kb` 表头存在但行为空。
- 根因：`pyproject.toml` 中 `mcp` 依赖未写版本约束，`pip install .` 解析到 2.x；2.x 移除了 `mcp.server.fastmcp.FastMCP`（重命名为 `MCPServer`），导致 `app/interfaces/mcp/server.py` 第 3 行 import 失败，uvicorn 启动崩溃。问题跨越 `pyproject.toml` + `app/interfaces/mcp/server.py` + Dockerfile 镜像构建，build 阶段无语法错误但运行时崩溃。
- 教训：直接进入容器运行时的重要依赖（不仅是 Web 框架，也包括像 mcp 这类带语言层 API 的 SDK）必须在 `pyproject.toml` 中 pin 上界，并在 `tests/` 下加约束测试防止未来被无意识放宽。docker 镜像构建只检测语法和依赖解析，跨主版本 API 破坏不会在 build 阶段暴露。
- 来源：2026-08-29 Bug 修复任务（`docker/restart.sh` 失败）。

---

### P002: dbapi 自动埋点只拦截 cursor() 派生游标，SQLite 直连 execute 对观测不可见
- 现象：OTel sqlite3 instrumentation 安装后，业务 SQL 调用没有生成任何 CLIENT span，无报错。
- 根因：`opentelemetry-instrumentation-sqlite3` 的 dbapi 包装只在 `.cursor()` 派生的游标上生效，`connection.execute(...)` 内部短路径不经过被包装的 cursor 工厂。跨 `app/infrastructure/persistence/sqlite_store.py` + `app/infrastructure/observability/instrumentors.py` 两个文件才定位。
- 教训：接入 dbapi 类自动埋点时，先核实 interception 的实际挂载点（读安装的 instrumentation 源码而非只看文档）；存储层若存在连接级快捷方法，需要统一改走可被拦截的调用形态，且这类行为等价改写要在计划中显式记录偏差。
- 来源：2026-10-30 OTel 可观测功能迭代（T5/T8，sqlite_store.py 23 处等价改写）。

### P003: 入口脚本不在分层 AST 守护覆盖内，Infrastructure import 例外会静默扩散
- 现象：结果验收扫描发现 `app/mcp_stdio.py` 顶层直接 import `app.infrastructure.observability`，而 `tests/test_architecture_boundaries.py` 的三层包扫描全部通过。
- 根因：AST 守护只覆盖 domain/application/interfaces 三个包，`app/` 顶层入口脚本（main.py/composition.py/mcp_stdio.py）无断言；组合根豁免被顺手复制到非组合根入口，形成 main.py 之外的第二个 Infrastructure 取用点。
- 教训：分层守护的"未覆盖区域"就是例外扩散的入口；为入口脚本补"Infrastructure 符号仅可经组合根函数注入"的守护断言后，同类违规才能在 CI 前暴露。新增顶层模块时同步检查其是否落在既有守护范围。
- 来源：2026-10-30 OTel 可观测功能迭代（Phase 5 扫描 D1，已修复并补 test_entry_scripts_import_infrastructure_only_via_composition）。


### P004: 相邻 Python 仓库同名 app 包会让临时生成脚本误读另一项目
- 现象：N-KB指标映射生成脚本正常执行，但表内出现N-Agent的nagent与inference指标，JSON配置结构校验未发现。
- 根因：临时脚本位于locals/harness_tmp，Python的脚本目录取代仓库根进入sys.path首位，环境中另一个editable安装的同名app包被解析；跨临时生成脚本、语义registry和生成的映射文档才定位。
- 教训：跨仓同名包环境执行临时脚本必须显式固定项目根sys.path或以正确模块入口运行，并校验导入模块来源；生成文档应对真实registry逐项断言，不能只验文件格式。
- 来源：2026-10-30 补全OTel观测，Grafana spec review发现并修正；已增加registry映射回归。

### P005: FastAPI 路由树版本差异会让观测白名单漏掉全部 include_router 业务
- 现象：N-Agent调用/retrieval/search已有CLIENT Span，但N-KB内部检索Span成为另一Trace的根，没有入站SERVER；宿主传播单测全部通过。
- 根因：容器FastAPI0.162将include_router保留为无path/methods的_IncludedRouter树节点，_derive_allowlist只枚举顶层app.routes而跳过业务；宿主FastAPI0.136使用平铺路由，既有测试又以直接注册路由为主，掩盖部署版本差异。
- 教训：遍历业务路由使用公开iter_route_contexts取得带完整prefix的方法/模板，兼容早期effective_route_contexts和平铺版本；传播测试必须覆盖实际include_router/嵌套prefix，并在运行容器版本做真实SDK/ASGI复现。应先检查SERVER是否创建，再判断上游是否漏传traceparent。
- 来源：2026-10-30 联合调试04a0c0087de9bb79f59ad7704a7f2889与fd4ee364ceeae4857191d37a016fdb97；容器修前复现0SERVER，本地新增回归先失败后通过。
- 验证补充：本轮新增5个回归case，全部672 passed/2 skipped；修复镜像重建与健康检查通过。实际跨容器Tempo诊断链9240565fe85932e803071738e6e44b61证明双服务同Trace且CLIENT→SERVER直接父子关系正确。

### P006: 跨仓共享资产的对比测试必须把契约范围限定在共享前缀
- 现象：test_dashboard_layout_components_and_filters_match_nagent在干净main上失败，nagent-overview.json未动过N-KB侧任何文件。
- 根因：相邻n-agent提交46b62dc向nagent-overview.json新增2个agent域面板（11->13），N-KB测试将自身前11面板gridPos与reference全量面板比较，长度不等恒不可满足；实际共享头部区域（前11面板）逐项仍对齐，分歧仅在reference新扩展的领域行。
- 教训：跨仓共享栈的对比测试应显式声明契约覆盖范围（共享前缀/共享行），reference在共享区之后的领域扩展不镜像；否则相邻仓库正常演进就会打破本仓测试，且失败信息（长度差）不指向真实漂移点。
- 来源：2026-11-02 修复debt-tracker D005；stash验证干净main复现，收窄断言至reference[:11]后6/6通过、全量756 passed。

### P007: 完成快照必须跨 Span 包装器保留准备属性的同次投影
- 现象：生命周期冻结的详情在 OTLP Log 有 original_bytes，Span Event 却丢失该元数据；单独 Application 测试通过，真实 SDK 多出口对照才复现。
- 根因：Application 逐字段写入已脱敏内容，Infrastructure Span 包装器将各字段当作新的未准备输入再次投影，正文与长度元数据被拆散；日志桥又有独立完成事件入口，会与生命周期重复发布。
- 教训：操作唯一拥有完成事实，日志桥只发布受信 Log；组合根注入准备属性作用域，生命周期在其中批量写入同一不可变快照的属性与 Event。必须对照真实 SDK 的 Span/Event/Log/stderr 元数据，并覆盖未采样与默认日志路径，不能只断言 Span 存在。
- 来源：2026-11-04 规范化观测语义，真实 SDK parity RED→GREEN；修正 completion_scope 与 bridge 所有权后通过。

### P008: Langfuse 共享项目修改必须验证运行中 Collector 的实际挂载与入库归属
- 现象：源码已改为共享项目，N-Agent Trace 仍缺少 N-KB span。
- 根因：Collector 继续挂载旧内容寻址配置，KB exporter 使用旧独立项目凭据；本地旧凭据又被新 dotenv 校验拒绝，启动解释器 3.11 也无法解析配置生成器。
- 教训：先对照双服务 Trace/parent ID，再查询 Langfuse 实际 project_id。配置发布需验证解释器、清理废弃键、生成配置、validate 并重建 Collector，最后查新请求入库结果；仅修改源码或外链不足以证明迁移生效。
- 来源：2026-11-08 排查 Trace a921c4237c3658a181a5a88802141e48；诊断链 11081108110811081108110811081108 验证修复。
