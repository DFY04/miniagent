# P04 —— 记忆（短期 + 长期）

## 目标

给 Agent 装上"记忆"：
- **短期记忆**：记住这次对话，但取出来时不会撑爆上下文
- **长期记忆**：从外部文档里捞相关片段

## 你要写

### `memory/short_term.py` —— `ShortTermMemory`

| 方法 | 目的 |
|------|------|
| `__init__(max_tokens, llm=None)` | 记住预算 + 要不要用 LLM 摘要；准备空的消息列表 |
| `add(msg)` | 把消息记进来（**只管记，不压缩**） |
| `build()` | 返回"喂给模型用"的列表，**保证不超预算** |

**`build()` 的三种情况**：

```
没超预算          → 原样返回
超了 + 有 llm     → 用 context.compress（先摘要，再裁）
超了 + 没有 llm   → 用 context.truncate（直接裁）
```

**核心思想**：`ShortTermMemory` 自己**不实现压缩**，它只是"仓库 + 守卫"，压缩交给 `core/context.py`。

### `memory/long_term.py` —— `LongTermMemory`

| 方法 | 目的 |
|------|------|
| `__init__(retriever=None)` | 准备检索器（不传就用 qwen-agent 的 `Retrieval`） |
| `add_documents(paths)` | 登记一批可查的文档（累积 + 去重） |
| `retrieve(query)` | 捞相关片段，返回**文本** |

**核心思想**：`LongTermMemory` 自己**不做检索**，它是 qwen-agent `retrieval` 工具的**包装**（Wrapper）——
向量化、切块、打分排序这些活不重造。

**边界**：没有文档 / query 为空 → 返回 `''`，**不要报错**（"没有知识"是正常情况）。

## 验收

```powershell
.venv\Scripts\python.exe -m pytest miniagent/p04/test_p04.py -v
# 或
.venv\Scripts\python.exe -m miniagent.p04.test_p04
```

## 提示

- 两个类都支持**依赖注入**（`llm` / `retriever` 可以塞假的）—— 这样测试不用联网、不用装 RAG 重依赖。
- `retriever` 的契约：`retriever.call({'query': ..., 'files': [...]})` → 返回结果。
- 别在 `build()` 里重新实现裁剪 —— **直接调 `context.truncate` / `context.compress`**。
