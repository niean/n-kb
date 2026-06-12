<!-- SUMMARY: N-KB 的跨文件实现模式，包括依赖组装、文档上传入库、检索、标签过滤、外部依赖适配和测试模式 -->
# 关键代码模式

项目中反复出现但不易从单个文件推断的模式，供新功能实现时参照。

## 模式一：依赖组装

`app/main.py` 是唯一组装 Infrastructure 具体实现的位置。组装顺序：Settings -> SQLite store -> ObjectStore -> Qdrant VectorIndex -> Ollama EmbeddingProvider -> Parser/Splitter -> Application services -> HTTP routes。

Application service 构造函数只接收 Domain 端口，不接收具体 SQLite/Qdrant/Ollama/LlamaIndex 实现。

陷阱：不要为了方便在 Interfaces 或 Application 中直接实例化 Infrastructure 类。

## 模式二：文档上传入库

上传入库流程：

1. Interfaces 接收文件、source、tags。
2. Interfaces 转换为 Application command。
3. Application 校验文件类型、大小、标签格式。
4. Application 通过 ObjectStore 保存原文件。
5. Application 通过 DocumentRepository 保存文档、来源、原文和标签。
6. Application 创建 IndexJob。
7. Application 编排 parser/splitter/embedding/vector index 写入。
8. Application 更新文档状态和任务状态。

入库写向量前必须校验 chunk 数量、vector 数量和 chunk_id 对齐。重建或重新入库使用 `VectorIndex.replace_document(document_id, ...)` 按文档替换向量；Qdrant 写入失败时恢复 SQLite 中该文档旧 chunk 元数据。

陷阱：上传成功不等于索引成功；文档状态和索引任务状态需要分开表达。

删除文档流程：Interfaces 调用 `DocumentService.delete_document`；Application 先确认文档存在，再用 `VectorIndex.replace_document(document_id, [], [], {}, {})` 清理 Qdrant 向量，随后删除 ObjectStore 原文件和 SQLite 中 document/content/tags/chunks/index_jobs。

陷阱：管理页面删除不能只隐藏列表项，必须调用 DELETE API 并刷新列表，避免 UI 与 SQLite/Qdrant 状态不一致。

## 模式三：语义检索

检索流程：

1. Interfaces 接收 query、filters、top_k、min_score。
2. Application 调用 EmbeddingProvider 生成 query 向量。
3. Application 将标签过滤和可选 min_score 转换为 VectorIndex filter。
4. VectorIndex 查询 Qdrant；显式 min_score 通过 score_threshold 过滤低相关命中。
5. Application 将命中结果转换为 RetrievalResult。
6. Interfaces 返回稳定 JSON。

陷阱：Qdrant 原始 payload 不是 API 合约，不能直接透传；top_k 表示最多返回数量，不应由默认 score_threshold 截断，低相关过滤必须由调用方显式传入 min_score。

## 模式四：统一 key-value 标签

所有标签统一使用 `key=value` 模型。分类是固定 key：`category=<value>`。

过滤表达式先支持等值过滤：

```json
{"tags": {"category": "prd", "project": "n-agent"}}
```

后续可扩展为 and/or/not、前缀匹配和批量标签管理，但 Domain 初始模型仍保持 key-value。

陷阱：不要同时引入独立 category 字段和 tag category，避免双写不一致。

## 模式五：LlamaIndex 适配边界

LlamaIndex 可用于解析、切分、索引辅助或 retriever 组合，但必须包在 Infrastructure adapter 后面。adapter 对 Application 暴露项目自有模型，如 Chunk、RetrievalResult。

陷阱：不要把 LlamaIndex Document、Node、Retriever 类型放进 Domain 或 HTTP schema。

## 模式六：外部服务健康检查

健康检查分层：

- `/health`：只验证 n-kb 进程可用。
- `/health/dependencies`：验证 SQLite、Qdrant、Ollama embedding 依赖状态。
- Application 依赖健康检查端口，Infrastructure 实现具体探测。
- Ollama dependency health 必须确认配置的 embedding model 可用，例如 `bge-m3` 或等价 tag alias。

陷阱：基础 `/health` 不应因为 Qdrant/Ollama 暂时不可用而导致容器被误判为不可启动。

## 模式七：测试替身

Application 测试使用 fake repository、fake embedding provider、fake vector index 测试真实用例行为。

Infrastructure 测试验证 SQLite schema、Qdrant adapter 请求转换、Ollama adapter 响应解析等边界。

陷阱：不要只断言 mock 被调用次数；测试应覆盖输入输出行为和状态变化。

## 模式八：FE 安全文本渲染

FE 展示文档标题、原文、来源、标签、chunk 文本和检索片段时使用 textContent 或框架安全渲染，不拼接 innerHTML。

管理页采用 Dashboard-first 控制台：总览展示文档/入库/依赖状态，文档页负责上传、过滤、详情、原文和只读 chunk 可视化，检索页负责 query/top_k/min_score/过滤条件验证，健康页展示依赖状态；静态资源按职责拆分为 app shell、Design Token 样式、HTTP API helper、安全 UI helper、hash 导航和页面入口脚本；组件、布局、视觉和交互细则见 `.harness/framework/guides/10-guidelines-fe.md`。

陷阱：Markdown 原文和 chunk 文本默认按文本展示；如果后续支持 Markdown 预览，必须引入 sanitizer。chunk 可视化是只读调试能力，不应在 FE 中直接编辑 SQLite/Qdrant 数据。
