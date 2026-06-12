<!-- SUMMARY: N-KB 的核心术语，包括文档、来源、标签、分类、chunk、embedding、向量索引、入库任务和检索结果 -->
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
