# 技术债追踪

| ID | 描述 | 优先级 | 来源计划 | 发现时间 | 状态 |
|----|------|--------|---------|---------|------|
| D001 | pytest 运行时 FastAPI TestClient 触发 StarletteDeprecationWarning，后续需迁移到 httpx2 兼容路径并清除警告 | low | N/A | 2026-08-29 | resolved |
| D010 | n-agent 观测启动入口随 pyenv 选用 Python 3.11，但现有 langfuse-config.py 的 f-string 表达式需要 Python 3.12+；本次用绝对路径 Python 3.13 完成配置发布，后续需统一入口解释器或恢复 3.11 兼容 | medium | N/A | 2026-11-08 | open |

| D002 | FastAPI/Starlette依赖未锁定，宿主0.136与容器0.162的路由树语义不同；本次已修复观测兼容并验证双版本，后续需统一依赖约束/部署版本回归环境 | medium | N/A | 2026-10-30 | open |
| D006 | 自动埋点曾将 SQLite CLIENT 按 HTTP 生命周期发布完成事件；规范化任务已按 instrument scope 与 DB 属性区分，增加不产生 HTTP 完成事件回归 | medium | plan-261104-normalize-observability.md | 2026-11-04 | resolved |

---

## 维护规则

本轮用户明确允许延后的小项：

| ID | 描述 | 优先级 | 来源计划 | 发现时间 | 状态 |
|---|---|---|---|---|---|
| D003 | HTTP请求/回执沿流捕获、gzip/deflate与早关主链路已补齐；multipart增量字段/文件元信息细化、完整压缩/取消压力矩阵继续延后 | medium | plan-261030-enrich-telemetry-details.md | 2026-10-31 | open |
| D004 | 隔离Collector转换测试、完整跨入口异常/取消/零采样实机矩阵及Grafana双向UI点击验收延后；本轮优先真实业务Trace/Log主链路 | low | plan-261030-enrich-telemetry-details.md | 2026-10-31 | open |
| D005 | tests/test_grafana_dashboards.py::test_dashboard_layout_components_and_filters_match_nagent 在干净 main 上失败，共享大盘布局/过滤器与相邻 n-agent 漂移，需对齐 | medium | N/A | 2026-11-02 | resolved |
| D007 | n-agent tests/e2e/test_langfuse_smoke.py 真实 fork 测试的 HEAD 基线取自 git show HEAD，T1 提交后退化为自比较，需改用固定基线快照或发布 tag；另缺 --add-host=host.docker.internal:host-gateway（Linux 兼容）、run_fork 就绪探测可能重复投递、decode_export 死代码（fake_get_kb 已随单共享项目改造删除） | low | plan-261107-langfuse-parallel-observability.md | 2026-11-08 | open |
| D008 | N-KB observations-otel.js Object.assign 对字符串型 links 会展开字符索引（safeHref 兜底拒绝，理论边界） | low | plan-261107-langfuse-parallel-observability.md | 2026-11-08 | open |
| D009 | n-agent manage-stack.py status 双项目探针延迟（服务挂起时最长约60秒）已随第二项目机制整体回退消除：Langfuse 改为单共享项目 n-agent-local-project、同组凭据，LANGFUSE_KB_PROJECT_* 与 ensure_kb_project/双探针全部移除 | low | plan-261107-langfuse-parallel-observability.md | 2026-11-08 | resolved |

1. 技术债来源：功能迭代、代码扫描、设计评审等渠道发现的问题
2. 代码扫描问题处理：代码扫描发现的问题，如非本次变更新引入，应记录到本文件而非要求立即修复
3. 优先级定义：high-影响核心功能或用户体验，需尽快修复；medium-有替代方案，可排期修复；low-优化项，有空闲时处理
4. 状态流转：open -> in_progress -> resolved，resolved 状态保留 1 个月后归档
