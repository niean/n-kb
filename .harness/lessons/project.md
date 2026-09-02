<!-- SUMMARY: {{项目名称}}开发中的经验教训，AI自主维护 -->
# 项目教训

AI 自主维护，人工可通过提示或建议触发新增/修正。
项目教训绑定{{项目名称}}，不随 Harness 模板提取。

---

### P001: 关键 Python 依赖必须 pin 上界，否则上游破坏性升级会让容器无法启动
- 现象：`docker/restart.sh` 重建镜像后容器立刻退出，`curl /health` 失败，`docker compose ps n-kb` 表头存在但行为空。
- 根因：`pyproject.toml` 中 `mcp` 依赖未写版本约束，`pip install .` 解析到 2.x；2.x 移除了 `mcp.server.fastmcp.FastMCP`（重命名为 `MCPServer`），导致 `app/interfaces/mcp/server.py` 第 3 行 import 失败，uvicorn 启动崩溃。问题跨越 `pyproject.toml` + `app/interfaces/mcp/server.py` + Dockerfile 镜像构建，build 阶段无语法错误但运行时崩溃。
- 教训：直接进入容器运行时的重要依赖（不仅是 Web 框架，也包括像 mcp 这类带语言层 API 的 SDK）必须在 `pyproject.toml` 中 pin 上界，并在 `tests/` 下加约束测试防止未来被无意识放宽。docker 镜像构建只检测语法和依赖解析，跨主版本 API 破坏不会在 build 阶段暴露。
- 来源：2026-08-29 Bug 修复任务（`docker/restart.sh` 失败）。
