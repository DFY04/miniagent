# MiniAgent

一个自研的 **agent harness**（面试项目 / 教学项目）。

> 定位：不重造 LLM，只造 qwen-agent 没做的「骨架 + 生产特性」。
> LLM 调用直接复用 `qwen_agent.llm.get_chat_model`，其余全部自研。

---

## 现状（一句话）

从"最小可跑骨架"出发，逐层补上了生产级 Agent 需要的可靠性设施：
**事件埋点、Tracer、预算、重试、审批闸门、checkpoint/回滚、轻量沙箱、RAG 记忆、统一 CLI**。
评测 harness（M6）与多智能体（M7）尚未开始。

---

## 功能地图（M1 → P11）

| 编号 | 模块 | 文件 | 一句话 |
|------|------|------|--------|
| M1 | 主循环 + 工具注册表 | `core/agent.py` `core/tool.py` | think → act → observe 主循环，两级工具注册表 |
| P01 | 事件总线 / 埋点 | `core/hooks.py` `core/hooks_agent.py` | 主循环每个关键点 emit 事件 |
| P02 | 上下文截断 | `core/context.py` | token 超限时按优先级裁剪 |
| P03 | 摘要压缩 | `p03/` | 旧对话压成摘要，腾出窗口 |
| P04 | 短期记忆 | `memory/short_term.py` | 记得住对话，又不撑爆上下文 |
| —   | RAG 长期记忆 | `memory/embedders.py` `memory/vector_store.py` | Chroma + DashScope 向量检索 |
| P05 | Tracer | `runtime/tracer.py` | 事件落 JSONL，可回放 |
| P06 | 预算 | `runtime/budget.py` | token/步数上限，超了优雅停 |
| P07 | 重试 + 死循环检测 | `runtime/retry.py` | 指数退避重试 + 连续重复调用止损 |
| P08 | 统一 CLI | `cli.py` | 一个入口：run / replay / eval |
| P09 | 审批闸门 | `runtime/approval.py` | 危险操作执行前人工确认 |
| P10 | checkpoint / 回滚 | `checkpoint/` `runtime/rollback.py` | 改文件前存档，失败可回滚 |
| P11 | 轻量沙箱 | `tools/sandbox.py` | 子进程 + 超时 + 静态检查，跑 LLM 代码 |
| M6  | 评测 harness | — | ⬜ 未开始 |
| M7  | 多智能体 | — | ⬜ 未开始 |

---

## 目录结构

```
miniagent/
  cli.py               # ★ 统一入口：run / replay / eval
  llm.py               # LLM 适配器：复用 qwen-agent，只做格式转换
  core/
    message.py         # Message / FunctionCall 数据结构
    tool.py            # ★ 两级注册表 + @tool 装饰器 + BaseTool
    agent.py           # ★ 主循环 Agent.run()
    hooks.py           # 事件类型 + 控制流信号
    hooks_agent.py     # 带埋点的主循环（副本）
    context.py         # token 估算 + 上下文截断
    protocols.py       # Protocol 类型（给 mypy 用）
    mem_agent.py       # 带记忆的主循环
  runtime/
    tracer.py          # P05 事件落 JSONL + 回放
    budget.py          # P06 token/步数预算
    retry.py           # P07 重试 + 死循环检测
    approval.py        # P09 审批闸门
    rollback.py        # P10 回滚闸门
    practice.py        # 练习 / 参考实现
    tracer_reference.py
  checkpoint/
    checkpoint.py      # P10 快照存档
    *.py               # demo + 自测
  memory/
    short_term.py      # P04 短期记忆
    embedders.py       # 文本→向量（Hash 玩具 / DashScope 真模型）
    vector_store.py    # Chroma 向量库 + RAG 检索
  tools/
    data_tools.py      # SQLite 只读查询
    demo_tools.py      # 玩具工具（now / add）
    file_tools.py      # P10 文件工具（声明写路径）
    sandbox.py         # P11 轻量沙箱
  p02/ p03/ p04/       # 阶段性里程碑 + 各自验收
  examples/            # 各类 demo（数据 agent / hooks / checkpoint / rag）
  tests/               # 离线验收测试（pytest，不联网不烧 API）
```

---

## 数据流（主循环）

```
用户问题
   │
   ▼
Agent.run(messages)
   │  ① 注入 system 消息
   ▼
 for step in range(max_steps):
     reply = llm.chat(messages, tools=tool_schemas)   # 把工具说明书喂给模型
     │
     ├── reply 没有 function_call  → 结束，返回最终答案
     └── reply 有 function_call
            │  ② 从 tool_map 取出工具实例，执行
            ▼
         observation = tool.call(arguments)
            │  ③ 作为 function 消息追加回 messages（observe）
            └── 继续下一轮
```

---

## 关键设计决策（面试可讲的点）

### 1. 两级工具注册表
- **全局 `TOOL_REGISTRY` 存「类」**，**Agent 的 `tool_map` 存「实例」**。
- 类定义一次全局共享；实例在创建 Agent 时按需带 `cfg`，不同 Agent 互不影响。
- 对应 qwen-agent 的 `TOOL_REGISTRY` + `function_map`，这里做了显式简化。

### 2. 复用而非重造 LLM 层
- 直接 `from qwen_agent.llm import get_chat_model`。
- 只为适配器写「消息格式双向转换」，不碰 HTTP / 流式 / 方言。

### 3. 主循环的两个改进（相对 qwen-agent）
- **显式 `max_steps`**：防死循环，超限时明确报错，而不是静默返回。
- **工具异常被捕获**：工具执行失败转成字符串 observation 回给模型，整个 run 不崩。

### 4. 可注入的 LLM（离线测试）
- `Agent(llm=...)` 接受任何有 `chat(messages, tools)` 的对象。
- 因此可以用假 LLM 做**离线测试**，跑 CI 不烧 API 额度。

### 5. 事件总线 + Tracer（可观测性）
- 主循环每个关键点 `emit` 事件，`runtime/tracer.py` 把它落成 JSONL。
- 跑完可以 `cli replay` 回放，定位"这轮为什么慢 / 为什么调了 3 次工具"。

### 6. 三层防护的轻量沙箱（P11）
- 静态检查（AST）→ 超时 kill → 目录隔离，三件事互相不覆盖。
- 诚实的边界：防"手滑"，不防"恶意绕过"；真隔离要容器。

### 7. 凡 LLM 可控的数值都 clamp
- LLM 能传 `timeout=99999` 拆掉超时保护，所以服务端一律夹到安全区间。
- 同理：token/步数预算、重试次数。

### 8. 工具"永不抛异常"契约
- `call()` 永远返回字符串，失败也包装成 observation 回给模型。
- 否则工具一抛异常，整个 Agent 主循环被炸。

### 9. 声明式写路径（P10 checkpoint 的基石）
- 工具用 `declare_writes()` 声明"我会改哪些文件"，Agent 在动手**前**存档。
- 工具不用认识 Checkpoint，解耦。

---

## 怎么跑

在 qwen-agent 仓库根目录下（依赖它的 LLM 层）：

```bash
# 1. 全部离线测试（不需要网络 / API key）
.venv\Scripts\python.exe -m pytest miniagent/tests -v

# 2. 单个模块的验收，例如沙箱
.venv\Scripts\python.exe -m pytest miniagent/tests/test_p11_sandbox.py -v
.venv\Scripts\python.exe -m miniagent.tools.sandbox        # 8 组真机输出

# 3. 统一 CLI
.venv\Scripts\python.exe -m miniagent.cli --help
.venv\Scripts\python.exe -m miniagent.cli run "你的问题"
.venv\Scripts\python.exe -m miniagent.cli run "文档该怎么切分" \
    --rag miniagent/memory/asn.txt --trace trace.jsonl
.venv\Scripts\python.exe -m miniagent.cli replay trace.jsonl
```

> `run` 会真的调模型，需要一个可用的 LLM 配置（默认 `qwen-max` + `DASHSCOPE_API_KEY`，写在项目根目录 `.env` 里）。

---

## 依赖

- `qwen-agent` —— 复用 LLM 层（不重造）
- `chromadb` —— RAG 向量库
- `dashscope` —— embedding / 真模型调用
- `matplotlib` —— 沙箱画图

---

## 路线图

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| **M1** | 主循环 + 两级工具注册表 + LLM 适配器 | ✅ 完成 |
| **M2** | 上下文管理（截断 + 摘要）+ 短期记忆 + RAG | ✅ 完成 |
| **M3** | Tracer：事件落 JSONL，可回放 | ✅ 完成 |
| **M4** | 可靠性：预算、重试、死循环检测 | ✅ 完成 |
| **M5** | 审批闸门 + checkpoint/resume + 沙箱 | ✅ 完成 |
| **M6** | 评测 harness：用例集 → 自动打分 → 报告 | ⬜ 未开始 |
| **M7** | 多智能体 orchestrator | ⬜ 未开始 |

---

## 与 qwen-agent 的对应关系

| MiniAgent | qwen-agent |
|-----------|-----------|
| `core/tool.py` `TOOL_REGISTRY` / `@tool` | `tools/base.py` `TOOL_REGISTRY` / `register_tool` |
| `Agent.tool_map` / `register_tool()` | `agent.py` `function_map` / `_init_tool` |
| `core/agent.py` `Agent.run()` | `agents/fncall_agent.py` `FnCallAgent._run` |
| `core/message.py` `Message` | `llm/schema.py` `Message` |
| `llm.py` `LLMClient` | `llm/base.py` `BaseChatModel`（直接复用） |
| `tools/sandbox.py` `PythonSandbox` | `tools/code_interpreter.py`（真容器） |
