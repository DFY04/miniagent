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

### 长期记忆 —— **自建 RAG（不走 qwen-agent 的 Retrieval）**

> 这一节和最初计划不同：最初打算“包装 qwen-agent 的 Retrieval，不重造”。
> 实际实现时选了**自己造一条完整的 RAG 链路**（能亲手碰到分块/向量/相似度/入库），
> 所以没有 `LongTermMemory` 这个门面类，也没有 `memory/retriever.py`。

| 零件 | 文件 | 干什么 |
|------|------|------|
| 切块 + 入库 + 检索 | `memory/vector_store.py` `DashScopeChromaRAG` | 读文件 → 按空行切 → 调 embedding → 存 Chroma → 查询 |
| 向量化 | `memory/embedders.py` | `HashEmbedder`（离线玩具，测试用）/ `DashScopeEmbedder`（真模型） |
| 知识注入 | `core/mem_agent.py` `run()` 开头 | 每轮进度检索一次，把片段**并进原来那条 system**后发给模型 |

**边界**：空库 / 空 query / 检索报错 → 都不注入知识，也不报错（降级为“没资料”）。

**为什么并进 system 而不是新插一条**：DashScope 拒绝多条 system，实测报错：
`The input messages must contain no more than one system message.`

## 验收

```powershell
# 短期记忆（全离线）
.venv\Scripts\python.exe -m pytest miniagent/p04/test_p04.py -v

# 长期记忆（会真调 DashScope，需联网 + API KEY）
.venv\Scripts\python.exe -m miniagent.examples.agent_hooks_demo
```

验收点：
- [x] 超预算时 `build()` 自动压缩（6 条用例）
- [x] 能从 `memory/asn.txt` 里检索出相关片段并答出内容
- [x] 多轮对话里短期记忆能记住上一轮（demo 里问“我叫什么名字”）

## 提示

- `ShortTermMemory` 支持**依赖注入**（`llm` 可以塞假的）—— 这样测试不用联网。
- 别在 `build()` 里重新实现裁剪 —— **直接调 `context.truncate` / `context.compress`**。
- `persist_path` 写相对路径会跟着 cwd 走。固定绝对路径，否则 PyCharm 和命令行会指到两个不同的库。
