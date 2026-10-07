# MiniAgent

一个自研的 **agent harness**（面试项目）。

> 定位：不重造 LLM，只造 qwen-agent 没做的「骨架 + 生产特性」。
> LLM 调用直接复用 `qwen_agent.llm.get_chat_model`，其余全部自研。

---

## M1 范围（当前已完成）

一个能跑通的最小 agent 骨架：

- ✅ 自己的 `Message` / `FunctionCall` 数据结构
- ✅ **两级工具注册表**：全局 `TOOL_REGISTRY`（名字→类）+ 每个 Agent 的 `tool_map`（名字→实例）
- ✅ `@tool` 装饰器（登记工具类）
- ✅ Agent 主循环：think → act → observe → 终止（显式 `max_steps` 上限）
- ✅ LLM 适配器：包装 qwen-agent，做消息格式双向转换
- ✅ demo：一个能查 SQLite 的数据分析 Agent

---

## 目录结构

```
miniagent/
  core/
    message.py      # Message / FunctionCall（对齐 OpenAI 的四要素）
    tool.py         # ★ 两级注册表：TOOL_REGISTRY + @tool 装饰器 + BaseTool
    agent.py        # ★ 主循环 Agent.run()：think -> act -> observe -> 终止
  llm.py            # LLM 适配器：复用 qwen-agent，只做格式转换
  tools/
    data_tools.py   # list_tables / sql_query（SQLite 只读查询）
  examples/
    m1_data_agent.py    # 真实 LLM demo
  tests/
    test_m1_offline.py  # 假 LLM 离线测试（不需要网络/API key）
```

---

## 数据流

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
- 为什么分开？类定义一次全局共享；实例在创建 Agent 时按需带 `cfg`，不同 Agent 互不影响。
- 对应 qwen-agent 的 `TOOL_REGISTRY`（`tools/base.py`）+ `function_map`（`agent.py`），但这里做了显式简化。

### 2. 复用而非重造 LLM 层
- 直接 `from qwen_agent.llm import get_chat_model`。
- 只为适配器写「消息格式双向转换」，不碰 HTTP / 流式 / 方言。

### 3. 主循环的两个改进（相对 qwen-agent）
- **显式 `max_steps`**：防死循环，超限时明确报错，而不是静默返回。
- **工具异常被捕获**：工具执行失败转成字符串 observation 回给模型，整个 run 不崩（qwen-agent 部分路径会直接抛）。

### 4. 可注入的 LLM
- `Agent(llm=...)` 接受任何有 `chat(messages, tools)` 的对象。
- 因此可以用假 LLM 做**离线测试**（跑 CI 不烧 API 额度）。

---

## 怎么跑

在 qwen-agent 仓库根目录下：

```bash
# 1. 离线测试（不需要网络 / API key）
.venv\Scripts\python.exe -m miniagent.tests.test_m1_offline

# 2. 真实 LLM demo（需要一个可用的 LLM 配置，默认 qwen-max + DASHSCOPE_API_KEY）
.venv\Scripts\python.exe -m miniagent.examples.m1_data_agent
# 也可以自带问题：
.venv\Scripts\python.exe -m miniagent.examples.m1_data_agent "2024 年销售额最低的城市是哪个？"
```

预期输出（demo）：

```
用户: 2023 年销售额最高的城市是哪个？

  [调用工具] list_tables({})
  [工具结果] list_tables:
    表 sales: city(TEXT), year(INTEGER), amount(REAL)
  [调用工具] sql_query({"query": "SELECT city FROM sales WHERE year=2023 ORDER BY amount DESC LIMIT 1"})
  [工具结果] sql_query:
    city
    ----
    杭州

助手: 2023 年销售额最高的城市是杭州。
```

---

## 路线图（后续里程碑）

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| **M1** | 主循环 + 两级工具注册表 + LLM 适配器 | ✅ 完成 |
| M2 | 上下文管理（截断 + 摘要）+ 短期记忆 + RAG 注入 | ⬜ |
| M3 | **Tracer**：每次 LLM/工具调用落 JSONL，可回放 | ⬜ |
| M4 | **可靠性**：预算（token/步数）、重试、死循环检测 | ⬜ |
| M5 | **审批闸门**（危险 SQL 前人工确认）+ checkpoint/resume | ⬜ |
| M6 | **评测 harness**：用例集 → 自动打分 → 报告 | ⬜ |
| M7 | 多智能体 orchestrator | ⬜ |

---

## 与 qwen-agent 的对应关系

| MiniAgent | qwen-agent |
|-----------|-----------|
| `core/tool.py` `TOOL_REGISTRY` / `@tool` | `tools/base.py` `TOOL_REGISTRY` / `register_tool` |
| `Agent.tool_map` / `register_tool()` | `agent.py` `function_map` / `_init_tool` |
| `core/agent.py` `Agent.run()` | `agents/fncall_agent.py` `FnCallAgent._run` |
| `core/message.py` `Message` | `llm/schema.py` `Message` |
| `llm.py` `LLMClient` | `llm/base.py` `BaseChatModel`（直接复用） |
