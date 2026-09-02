<!-- SUMMARY: N-KB项目的原始需求列表，人工维护、禁止AI修改 -->
# 产品需求 - 迭代演进

## 约束
本文仅供自然人使用，未经人工确认、禁止AI阅读或修改。

## 需求列表
无论何时，你都必须遵循DDD分层规范。

[20260612]
- NFR
  - 源码：检索结果，score是怎么定义的(结论：由Qdrant定义)
- FR
  - 文档：管理页面，支持删除文档
  - 管理：设置网站图标Favicon
  - 管理：左导菜单，设置独立URL、方便刷新浏览器
  - 管理：按照前端项目规范，重构管理页面；页面布局保持现状
  - 文件：文件列表，只保留文件名称、录入时间、操作，其它信息(VDB入库状态、ID、Tags等)放到右侧详情的上部
  - 检索：Min score，默认值从0.3调整为0.5
- Issue
  - 检索：设置Top5，实际最多展示2个
  - 检索：查询结果，完全不相关

[20260613]
- FR
  - 检索：提供标准的MCP接口。在此之前，制定MCP规范标准，如使用的代码框架等，尽量复用流行开源方案

[20260615]
- FR
  - 接口：MCP，改用标准的代码库（替代裸写），参考n-agent /Users/niean/code/github.com/niean/n-agent
  - 接口：MCP，支持stdio传输类型

[20260617]
- FR
  - 接口：MCP，干掉检索工具

[20260829]
- FR
  - 前端：左导样式优化。参考n-agent的左导样式，更新n-kb左导。n-agent /Users/niean/code/github.com/niean/n-agent
  - 左导：n-kb左导，宽度、字体都明显大于n-agent，需要调整n-kb使跟n-agent一致

[20260902]
- HE
  - HE：参照最新版本，升级本项目的Harness框架，识别和修正冲突，项目私有 knowledge/ 只做兼容性修正(不覆盖其业务语义)。新版Harness所在目录是：/Users/niean/code/github.com/niean/harness-tpl。建议步骤：S1.分析新老DIFF，S2.等待用户确认升级方案、补全信息，S3.执行。
- FR
  - 前端：左导样式优化，包括左导栏宽度、字体大小、图标大小，设置调整、使跟n-agent一致。n-agent /Users/niean/code/github.com/niean/n-agent
  - 前端：左导样式优化，检索、健康两个菜单间增加分隔符。使用迭代功能WF、三方审阅使用cc


---

[待办]
- FR
- NFR

