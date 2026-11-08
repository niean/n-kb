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

[20261029]
- FR
  - 观测：[KF]补全OTel可观测。按照业界规范，给N-KB补全内部观测，要求遵循OTel规范，你应该严格参考N-Agent的规范、方案、教训；同时，我希望N-Agent访问N-KB能够Trace串起来
    - 迭代：观测组件拆分为独立网络空间，compose配置请同步修改
    - 迭代：观测，跟N-Agent对比，包括左导菜单图标、层级、OTel页面样式+FE组件，不要自己造轮子。你先学习N-Agent，然后按照流程执行、免人工审批
    - 迭代：docker，观测相关的配置放到目录 docker/observability/ 下
    - 迭代：n-kb，补全OTel观测缺失，包括OTel页面中缺少的、Grafana大盘配置等；Grafana大盘配置，可以放到n-agent的docker/observability/目录
    - 迭代：Trace，属性缺少必要的payload参数，也缺少事件关联；Log，缺少payload参数、结果等信息。跟N-Agent的早期版本问题很像，参考它的经验、丰富信息

[20261031]
- FR
  - 前端：左导菜单，字体大小调整、跟N-Agent左导一致
  - 观测：OTel页面，页面内容块、字段等，要跟N-Agent OTel页面严格一致

[20261102]
- FR
  - 观测：左导菜单，健康页改到左导`观测-组件`页，然后干掉健康做到一级菜单。要求遵循项目FE规范

[20261104]
- FR
  - 观测：观测语义规范化，NKB O11y Semantic Contract。参照N-Agent，定义N-KB的观测语义，完成代码修改；N-Agent观测语义参见：/Users/niean/code/github.com/niean/n-agent/.harness/knowledge/07-observability.md 对应章节

[20261107]
- FR
  - 观测：[KF]引入Langfuse组件，尽量复用OTel组件(如Collector)；现有的OTel能力全部保留，跟Langfuse并存。观测语义等，完全Follow N-Agent。

[20261108]
- FR
  - 前端：左导收起时hover提示，一级菜单图标要有hover，告知用户这是什么菜单；同时，左导收起后二级菜单不弹出、无法点击，一并修复。前端的左导应该非常通用，建议跟N-Agent对齐、复用它的公共组件
  - 观测：N-KB 实际 Ollama 向量请求记录 gen_ai.operation.name=embeddings，但当前 Collector 将其投影为 generation，请修正

---

[待办]
- FR
- NFR

