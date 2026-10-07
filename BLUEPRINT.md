# MiniAgent 设计蓝图（最终态）

> 这是本项目的"北极星"文档：描述 **M1 → M7 全部完成后的终态架构、设计决策与验收标准**。
> 当前进度见文末「里程碑路线图」。README 讲"现在是什么"，本文讲"最终要做成什么"。

---

## 0. 项目定位

**一句话**：一个自研的 **agent harness** —— 不重造 LLM，只造 qwen-agent 没做的「骨架 + 生产特性」。

**面试目标**：证明三件事

1. 我**懂框架内部**（agent loop / 工具注册 / 记忆 / 多智能体怎么造）；
2. 我**懂生产**（可观测、可靠、可控、可评测）；
3. 我**有取舍判断**（知道什么该复用、什么必须自研、什么故意不做）。

---

## 1. 设计哲学

### 复用（不重造）

| 复用对象 | 为什么 |
|---------|--------|
| `qwen_agent.llm.get_chat_model` | HTTP / 流式 / function-calling 方言差异，不值得重造 |
| `qwen_agent.tools.*`（web_search / retrieval 等） | 工具是"耗材"，重造成本高、面试价值低 |
| qwen-agent 的 RAG 检索链（DocParser / search） | 向量化、切块、打分是成熟轮子 |

### 自研（这才是项目价值）

| 自研对象 | 为什么 qwen-agent 不够 |
|---------|----------------------|
| Agent 主循环 | 要加钩子（hooks）让运行时特性可插拔 |
| 工具注册表 | 要显式设计 + 可测 |
| **Tracer** | qwen-agent 没有结构化全链路 trace |
| **Budget / Retry** | 没有 token/成本预算、死循环检测 |
| **Approval 闸门** | 没有人工审批 HITL |
| **Checkpoint / Resume** | 没有会话快照恢复 |
| **Eval harness** | 没有通用评测框架 |

### 非目标（故意不做）

- ❌ 自己的向量库 / embedding 服务
- ❌ 多模态输入输出（文本-only）
- ❌ Web UI（用 CLI；需要时复用 qwen-agent 的 WebUI）
- ❌ 模型训练 / 微调
- ❌ 堆工具数量（demo 只要 3~5 个）

---

## 2. 最终架构总览

```
┌──────────────────────────────────────────────────────────────┐
│  L5 应用层     agents/data_analyst.py  （领域 agent）           │
├──────────────────────────────────────────────────────────────┤
│  L4 编排层     multiagent/orchestrator.py （多 agent 编排）      │
├──────────────────────────────────────────────────────────────┤
│  L3 记忆层     memory/short_term.py · memory/long_term.py       │
├──────────────────────────────────────────────────────────────┤
│  L2 运行时层   runtime/tracer · budget · retry · approval ·     │
│                checkpoint      ← 通过 hooks 挂进主循环          │
├──────────────────────────────────────────────────────────────┤
│  L1 循环层     core/agent.py · core/context.py · core/hooks.py  │
├──────────────────────────────────────────────────────────────┤
│  L0 基础层     core/message.py · core/tool.py · llm.py          │
├──────────────────────────────────────────────────────────────┤
│  横切          tools/ · eval/ · cli.py                          │
└──────────────────────────────────────────────────────────────┘
```

**关键结构决策：L2 的运行时特性不侵入 L1 的循环，而是通过 `core/hooks.py` 生命周期钩子挂载。** 这是本项目最重要的架构点（详见 §4）。

---

## 3. 最终目录结构

```
miniagent/
  core/
    message.py          # Message / FunctionCall                      [M1 ✅]
    tool.py             # BaseTool / TOOL_REGISTRY / @tool            [M1 ✅]
    agent.py            # Agent 主循环                                 [M1 ✅]
    hooks.py            # 生命周期钩子（on_llm_* / on_tool_* / ...）    [M2]
    context.py          # 上下文预算：截断 + 摘要 + 记忆注入             [M2]
  llm.py                # LLM 适配器（包装 qwen-agent）                [M1 ✅]
  runtime/
    tracer.py           # 结构化 trace → JSONL，可回放                 [M3]
    budget.py           # token / 成本 / 步数预算                       [M4]
    retry.py            # 重试 / 超时 / 死循环检测                      [M4]
    approval.py         # HITL 审批闸门（危险操作前暂停）                [M5]
    checkpoint.py       # 会话快照 / resume                            [M5]
  memory/
    short_term.py       # 短期记忆（对话窗口）                          [M2]
    long_term.py        # 长期记忆（复用 qwen-agent Retrieval）          [M2]
  tools/
    data_tools.py       # list_tables / sql_query（只读 SQLite）        [M1 ✅]
    sandbox.py          # 轻量 Python/SQL 沙箱（不依赖 Docker）          [M5]
  multiagent/
    orchestrator.py     # 多 agent 编排                                 [M7]
  agents/
    data_analyst.py     # 数据分析 agent（demo 载体）                   [M2]
  eval/
    harness.py          # 评测：跑用例 → 打分 → 出报告                   [M6]
    cases/              # 用例集（question / expected / scorer）         [M6]
    report.md           # 评测报告（自动生成）                           [M6]
  examples/
    m1_data_agent.py    # M1 端到端 demo                                [M1 ✅]
  tests/
    test_m1_offline.py  # 假 LLM 离线测试                               [M1 ✅]
  cli.py                # 统一入口                                     [M4]
  README.md             # 现状说明 + 怎么跑
  BLUEPRINT.md          # 本文件：最终态蓝图
```

---

## 4. 核心机制：Hooks（为什么这是架构关键）

主循环只发事件，不关心谁来处理：

```
Agent.run() 每步发事件：
  on_run_start(messages)
  on_llm_start(messages, tools)
  on_llm_end(reply, usage)              # usage: tokens / latency
  on_tool_start(name, args)
  on_tool_end(name, result, ok)
  on_step_end(step)
  on_run_end(reason)                    # reason: done | max_steps | approved_reject | error

订阅者（各自独立、可开可关）：
  Tracer      → 把每个事件写进 JSONL（可回放）              [M3]
  Budget      → 累加 token/成本/步数，超限就中断              [M4]
  Retry       → 包住 on_llm_start，失败时重试                 [M4]
  Approval    → 捕获 on_tool_start，危险工具先暂停等人工批准    [M5]
  Checkpoint  → 每个 on_step_end 存一次快照，可 resume        [M5]
```

**好处**：M3~M6 每个特性都是"新增一个订阅者"，不改主循环。这正是一个 harness 应有的可扩展性。

---

## 5. 各层详解

### L0 基础层

- `Message` / `FunctionCall`：对齐 OpenAI 四要素（role / content / name / function_call）。
- `BaseTool` / `TOOL_REGISTRY` / `@tool`：**两级注册表**
  - 全局 `TOOL_REGISTRY`：名字 → **类**
  - `Agent.tool_map`：名字 → **实例**（按 Agent 带 cfg）
- `llm.py`：包装 `get_chat_model`，只做消息格式双向转换。

### L1 循环层

- `Agent.run()`：think → act → observe → 终止，显式 `max_steps`。
- `context.py`：上下文预算
  - 超 token 就从最老的消息开始裁；
  - 裁掉的早期对话压成"摘要消息"；
  - 从长期记忆检索相关片段注入（记忆钩子）。
- `hooks.py`：生命周期事件总线（§4）。

### L2 运行时层（项目亮点）

| 模块 | 职责 | 面试可讲的点 |
|------|------|-------------|
| `tracer.py` | 每个 LLM/工具调用 → JSONL，带 trace_id/耗时/tokens | 出问题能回放复现 |
| `budget.py` | token/成本/步数上限，超限优雅中断 | 防跑飞、控成本 |
| `retry.py` | 指数退避重试 + 超时 + 死循环检测（重复工具+参数） | 可靠性 |
| `approval.py` | `on_tool_start` 命中危险名单 → 暂停等人工 y/n | HITL，安全 |
| `checkpoint.py` | 每步快照，崩溃后 resume | 长任务可恢复 |

### L3 记忆层

- `short_term.py`：对话窗口管理（配合 `context.py`）。
- `long_term.py`：**复用 qwen-agent 的 Retrieval**，做 RAG（不重造向量检索）。

### L4 编排层

- `orchestrator.py`：多个 Agent 协作（例如"分析师 + 批判者 + 总结者"），基于 L1 的 Agent 组合。
- 从简：先做顺序/路由两种模式，不做复杂协商。

### L5 应用层

- `agents/data_analyst.py`：演示载体 —— 数据分析 Agent。
  - 工具：`list_tables` / `sql_query` / `sandbox`（画图）
  - 也是 eval 的被测对象

### 横切

- `tools/`：工具集（自研 + 复用 qwen-agent）。
- `eval/`：评测 harness（§6）。
- `cli.py`：统一入口（跑 agent / 跑评测 / 回放 trace）。

---

## 6. 评测设计（M6，最值钱的部分）

### 用例格式

```jsonl
{"id": "q001", "question": "2023 年销售额最高的城市是哪个？", "expected": "杭州", "scorer": "contains"}
{"id": "q002", "question": "2024 年总销售额是多少？", "expected": "3650", "scorer": "numeric"}
{"id": "q003", "question": "列出所有城市名", "expected": ["杭州","上海","北京"], "scorer": "set_match"}
```

### 评测器

| scorer | 判定 |
|--------|------|
| `contains` | 答案包含 expected |
| `numeric` | 抽出数字，容差比较 |
| `set_match` | 集合相等 |
| `sql_match` | 执行标准 SQL，比对结果集 |

### 报告指标

- **准确率**（通过用例数 / 总数）
- **平均步数**、**平均 token**、**平均成本**
- **p50 / p95 延迟**
- **失败用例分类**（选错工具 / SQL 报错 / 答错 / 超步数）

> 报告输出到 `eval/report.md`。README 里能写："在 50 题评测集上准确率 X%，平均 Y 步，失败主要来自 Z"——这是面试最硬的一句话。

---

## 7. 完整数据流（终态）

```
用户问题
   │
   ▼
cli / data_analyst
   │
   ▼
Agent.run(messages)  ──on_run_start──▶ Tracer
   │  注入 system + 记忆（context.py / long_term）
   │
   ├─▶ on_llm_start ─▶ Retry(包住) ─▶ LLMClient.chat ─▶ qwen-agent
   │       │
   │       └─▶ on_llm_end(reply, usage) ─▶ Tracer / Budget
   │
   ├─ reply 无 function_call ──▶ on_run_end(done) ─▶ 最终答案
   │
   └─ reply 有 function_call
          │
          ├─▶ on_tool_start(name, args)
          │        │
          │        ├─ Approval：危险 → 暂停等人批 (y/n)
          │        └─ 非危险 → 直接放行
          │
          ├─ tool.call(...) （沙箱 / SQL）
          │
          ├─▶ on_tool_end(result, ok) ─▶ Tracer
          │
          ├─ 追加 function 消息（observe）
          ├─▶ on_step_end ─▶ Checkpoint(快照) / Budget(累加)
          └─ 继续下一轮（受 max_steps / budget 约束）
```

---

## 8. 关键设计决策（面试背诵区）

1. **两级工具注册表**：类全局共享，实例按 Agent 配置 → 避免状态串味。
2. **Hooks 而非继承**：运行时特性（trace/budget/approval）作为订阅者，主循环零侵入 → 可插拔、可测试。
3. **复用 LLM 层**：不重造 HTTP/流式/方言，把精力放在 harness 本身。
4. **可注入 LLM**：`Agent(llm=任意有 chat() 的对象)` → 离线 CI 测试不烧 API。
5. **显式预算 + 死循环检测**：agent 的典型翻车点是无限循环，必须工程化兜住。
6. **审批闸门在工具执行前**（不是执行后）：安全边界必须在副作用发生前。
7. **checkpoint 粒度 = 每步**：崩溃只需重跑最后一步，而不是整个任务。

---

## 9. 里程碑路线图（含验收标准）

| 里程碑 | 交付 | 验收标准 | 状态 |
|--------|------|---------|------|
| **M1** | 主循环 + 两级工具注册表 + LLM 适配器 | 离线测试通过；真实 demo 端到端查库作答 | ✅ 完成 |
| **M2** | 上下文预算 + 短期记忆 + RAG 注入 | 超长对话不超窗口；能检索历史作答 | ⬜ |
| **M3** | Tracer：调用落 JSONL + 可回放 | 一次 run 的完整 trace 能重放复现 | ⬜ |
| **M4** | 预算 / 重试 / 死循环检测 + CLI | 人为造死循环能被检出并止损 | ⬜ |
| **M5** | 审批闸门 + checkpoint/resume + 沙箱 | 危险 SQL 被拦；中断后可 resume | ⬜ |
| **M6** | 评测 harness + 用例集 + 报告 | 50 题自动跑分，出 report.md | ⬜ |
| **M7** | 多 agent 编排 + 一个多角色 demo | 多角色协作完成一个任务 | ⬜ |

---

## 10. 面试叙事（3 分钟版）

> "我自底向上读透了 qwen-agent，然后做了一个 MiniAgent harness。**我刻意不重造 LLM 和工具**——那些直接复用 qwen-agent。我造的是它没有的生产能力：
> ① 用**钩子**把 trace / 预算 / 审批 / checkpoint 做成可插拔的订阅者，主循环零侵入；
> ② 一个**评测 harness**，在 50 题数据集上把准确率、步数、成本、延迟都量出来了；
> ③ 安全上做了**工具执行前的审批闸门**和**每步 checkpoint**。
> 取舍上，我故意没做向量库和多模态——那不是这个项目的重点。"

**可能被追问 & 备好答案**：
- "为什么用钩子而不是继承？" → 继承会让特性互相耦合、难测试；钩子是可插拔的。
- "怎么检测死循环？" → 检测「相同工具 + 相同参数」重复 N 次，或步数/预算超限。
- "评测怎么保证公平？" → 固定数据集 + 固定模型 + 多次运行取均值。

---

## 11. 与 qwen-agent 的对应关系

| MiniAgent | qwen-agent |
|-----------|-----------|
| `core/tool.py` `TOOL_REGISTRY` / `@tool` | `tools/base.py` `TOOL_REGISTRY` / `register_tool` |
| `Agent.tool_map` / `register_tool()` | `agent.py` `function_map` / `_init_tool` |
| `core/agent.py` `Agent.run()` | `agents/fncall_agent.py` `FnCallAgent._run` |
| `core/message.py` `Message` | `llm/schema.py` `Message` |
| `llm.py` `LLMClient` | `llm/base.py` `BaseChatModel`（直接复用） |
| `runtime/tracer.py` | （qwen-agent 无对应） |
| `runtime/approval.py` | （qwen-agent 无对应） |
| `eval/harness.py` | （qwen-agent 无对应） |

> 最后三行，就是本项目的**差异化价值**。
