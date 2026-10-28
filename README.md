# MiniAgent

[![tests](https://github.com/DFY04/miniagent/actions/workflows/tests.yml/badge.svg)](https://github.com/DFY04/miniagent/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-314%20passed-brightgreen)
![Dependencies](https://img.shields.io/badge/runtime%20deps-1-lightgrey)
![Offline](https://img.shields.io/badge/tests-offline%20only-informational)
![License](https://img.shields.io/badge/license-MIT-green)

> 一个**从零实现**的 Agent harness：主循环、工具系统、上下文管理、多智能体编排、MCP 接入、沙箱、checkpoint 回滚、评测。
>
> **13 天 · 40 次提交 · 87 个 Python 文件 · 14,185 行 · 314 条离线测试全绿 · 不依赖 LangChain / 不依赖 Docker**

---

## ⚡ 30 秒：这个项目里我做了什么

| 我实现的模块 | 内容 | 体量 |
|---|---|---|
| **主循环** `core/` | think → act → observe 循环、两级工具注册表、事件总线、上下文截断与摘要、带记忆的主循环 | 9 文件 / 875 行 |
| **可靠性设施** `runtime/` | tracer（落盘 + 回放）、token 预算、重试与死循环检测、审批闸门、回滚 | 8 文件 / 783 行 |
| **多智能体** `multiagent/` | 串行 handoff 编排 + 并行 fan-out 编排（核心文件 806 行） | 6 文件 / 1,339 行 |
| **checkpoint** `checkpoint/` | 快照存档 + 「对话上下文 ↔ 磁盘文件」双世界回滚 | 7 文件 / 807 行 |
| **工具与沙箱** `tools/` | SQLite 查询、文件工具、轻量 Python 沙箱（515 行，三层防护） | 5 文件 / 628 行 |
| **MCP 接入** `mcp/` | stdio 客户端 + 适配器 + 测试用 server，接入后核心零改动 | 5 文件 / 482 行 |
| **记忆与检索** `memory/` | 短期记忆、embedding、内存向量库 | 4 文件 / 303 行 |
| **评测** `eval/` | 端到端 harness：准确率 / 步数 / token / p50·p95 / 失败分类 | 3 文件 / 473 行 |
| **测试** `tests/` | 离线、确定性、不烧 API | 26 文件 / 5,712 行 / **314 条** |

### 先说清楚来源（重要）

我不想让这个项目被高估，也不想被误判：

- ✅ **代码**：87 个 Python 文件是逐模块自己实现的，`git log` 40 次提交 / 13 天可查，能看到「写练习稿 → 失败 → 参考 → 再实现」的过程。
- ⚠️ **设计蓝图**：整体分层方式与各阶段的验收标准来自课程；`runtime/tracer.py` 直接采用了课程提供的参考实现（`tracer_reference.py` 与之内容一致）。
- 🎯 **自己改造最多、也最值得问的三块**：并行编排的写冲突检测、checkpoint 的双世界回滚、沙箱的三层防护。这三块建议直接读代码，不是照搬能写出来的。

> 一句话总结：**实现是我的，设计蓝图不全是我的，踩过的坑和改造是我的。**

---

## 🎯 三个最值得展开聊的技术点

如果你只有 10 分钟，读这三个，它们是这个项目里唯一不是「标准实现」的部分：

### 1. 并行编排：在派活**前**拦写冲突，在动手**前**拍快照

多 worker 并发最容易出的不是性能问题，是**两个 worker 改同一个文件**、以及**回滚时存成了改后版本**。这两件事在「跑起来之后再检查」时都已经晚了。做法见 [核心设计 §7](#7-多智能体两种编排策略是兄弟不是继承链)。

### 2. checkpoint：让「对话上下文」和「磁盘文件」回到同一个时刻

只回滚消息列表会留下改了一半的文件，只回滚文件会让模型以为自己还处在旧步骤。做法是把两个世界交给**同一个 step 号**驱动，工具通过 `declare_writes` 声明式声明自己要改哪些文件——工具不需要认识 Checkpoint，存档模块也不需要认识工具。见 [核心设计 §4](#4-checkpoint上下文--文件两个世界一起回滚)。

### 3. 沙箱：三层防护，每层只管一件互不覆盖的事

`while True: pass`、`import os; os.remove(...)`、`plt.savefig('C:/x.png')` —— 这三种危险代码**只有各自对应的那一层能拦住**。所以三层缺一不可，而不是「有了静态检查就不用超时」。见 [核心设计 §5](#5-轻量沙箱三层防护各管一件互不覆盖的事)。

---

## 技术决策记录（为什么这么选、代价是什么）

面试里最常被追问「为什么不用 X」，这里先把答案写下来。**每一条都有取舍，没有一条是白拿的。**

| 决策 | 我选了 | 替代方案 | 为什么 | 代价 |
|---|---|---|---|---|
| 横切关注点挂载方式 | **事件总线订阅者** | 直接写进主循环 | 5 个特性 × 主循环 = 5 处修改，主循环很快变意大利面 | 多一层间接，读代码要跳一次 |
| 并行用线程还是协程 | **线程**（执行器可注入） | asyncio | LLM 调用是 I/O，GIL 会释放；线程池代码更直白，测试可注入同步执行器 | 并发上限要自己管，没有结构化并发 |
| 同一成员能否并发 | **默认不并发** | 全并发 | LLM client 未必线程安全，同一成员的连续调用需要顺序 | 牺牲一部分并行度 |
| 工具异常的处置 | **永不抛异常，包装成 observation** | 抛出去终止任务 | 单次工具失败不该杀死整个任务，模型可以换策略 | 模型可能反复调用同一个失败工具（补了 LoopDetector） |
| 工具如何声明写操作 | **工具自己 `declare_writes`** | Agent 解析参数猜 | 工具最清楚自己会写什么；解耦 checkpoint 与工具实现 | 工具作者可能漏声明 |
| trace 计时 | **`perf_counter`** | `time.time` | 单调时钟，不被系统对时跳变影响 | 无法直接和 wall clock 对齐 |
| 重复调用检测 | **参数 md5**（跨进程稳定） | 内置 `hash()` | `hash()` 每次进程启动都变，跨进程无法复现 | 参数顺序不同会误判（已记录为已知限制） |
| MCP 的 async/sync 桥 | **后台线程常驻事件循环 + Future 桥接** | 全项目改 async | 核心 API 保持同步，接入第三方能力时 `agent.py` / `tool.py` 零改动 | 多一个线程，排错复杂 |

---

## 核心设计（这几条可以往深里问）

### 1. 横切关注点用事件总线挂载，而不是写进主循环

`tracer / budget / retry / approval / rollback` 都是**订阅者**，主循环只在关键点 `emit` 事件。

- **为什么**：5 个特性 × 主循环 = 5 处修改，主循环很快变成意大利面
- **代价**：多一层间接（读代码要跳一次）
- **不这么做会怎样**：每加一个特性都要改核心循环，回归风险极高

### 2. 两级工具注册表

全局 `TOOL_REGISTRY` 存「类」，每个 Agent 的 `tool_map` 存「实例」。

- 类定义一次全局共享；实例在创建 Agent 时按需带 `cfg`，不同 Agent 之间互不影响
- 对应 qwen-agent 的 `TOOL_REGISTRY` + `function_map`，这里做了显式简化

### 3. 工具「永不抛异常」+ 凡 LLM 可控的数值都 clamp

- `BaseTool.call()` 永远返回字符串，失败也包装成 observation 回给模型 → 工具炸了不会炸掉主循环
- LLM 能传的参数一律在服务端夹到安全区间（超时上限、预算、重试次数）→ 否则模型传个 `timeout=99999` 就把保护拆了

### 4. checkpoint：上下文 + 文件，两个世界一起回滚

- 工具用 `declare_writes(params)` **声明**「我这次会改哪些文件」，Agent 在动手**前**存档
- 工具不需要认识 Checkpoint（解耦）；回滚时把消息列表截断 + 文件从影子目录恢复
- 步号由 Agent 的循环变量显式驱动（`begin_step`），保证「上下文第几步」和「文件第几步」永远对齐
- 一步内可以调多个工具：`tool_calls` 用 `(step, seq)` 复合主键，旧库文件会自动迁移

### 5. 轻量沙箱：三层防护，各管一件互不覆盖的事

| 层 | 防什么 | 机制 |
|---|---|---|
| ① 静态检查（执行前） | 碰危险**东西** | AST 遍历，拦危险 import / 写方式 `open()` / 绝对路径 |
| ② 超时 kill（执行中） | 永远**不结束** | `subprocess.run(timeout=N)` |
| ③ 目录隔离（执行后） | 写到**哪儿** | `cwd=临时目录` + 扫目录收产出 |

- 行 `while True: pass` 一个危险 import 都没有 → 只有 ② 能拦
- 行 `import os; os.remove(...)` 一毫秒跑完 → 只有 ① 能拦
- 行 `plt.savefig('C:/x.png')` 合法且瞬间 → 只有 ③ 能拦

**诚实边界**：静态检查是「提高门槛」，不是安全边界。真隔离要容器（qwen-agent 的 `code_interpreter` 走的就是那条路）。

### 6. 记忆与上下文：三种手段分清楚各自的职责

- **截断**：超窗口时按条丢，便宜但会丢信息
- **摘要**：把早期消息压成一段，省 token 但可能失真
- **短期记忆 + RAG**：需要「记得住又查得到」时用，成本最高

三者的取舍写在 `core/context.py`；token 是本地粗估（中文 1 字 ≈ 1 token），只用于「超没超」判断。预算是**输入 + 输出一起记账**的，因为长上下文场景下输入 token 通常是输出的 10–50 倍。

### 7. 多智能体：两种编排策略是**兄弟**，不是继承链

`Orchestrator`（串行/共享上下文）与 `FanOutOrchestrator`（并行/上下文隔离）都继承 `MultiAgentHub`，但**互相不 import**，且有测试盯着这条约束。

| | 串行 `Orchestrator` | 并行 `FanOutOrchestrator` |
|---|---|---|
| 谁决定下一步 | LLM 每轮选人（handoff） | planner 一次拆完，执行器并发 |
| 上下文 | **共享**一条时间线 | **隔离**，每人只见自己的子任务 |
| 核心难题 | 身份转换：别人的话算 user 还是 assistant | 结果归位 / 并发上限 / 部分失败 / 写冲突 |
| 适合 | 辩论、评审、接力 | 批量处理、多路调研 |

两条路线的分水岭：**共享上下文换来「互相看见」，代价是「身份混乱」；隔离换来「简单干净」，代价是「看不见彼此」。**

并行侧额外做了三件「副作用」护栏：**派活前查写冲突**（跑起来再发现就晚了）、**动手前快照**（否则存的是改后版本）、**执行器可注入**（测试用同步执行器，不起线程、结果确定）。

### 8. MCP 接入：只加了一个适配器，核心零改动

`mcp/` 把任意 MCP server（stdio）的工具变成普通 `BaseTool`，`tools=mcp.tools` 就能挂：

| 要翻译的 | 怎么翻 |
|---|---|
| 工具名 | 净化成 `^[a-zA-Z0-9_-]{1,64}$`（可加 `prefix` 防多 server 重名） |
| `input_schema` | 只留 `type / properties / required` —— 丢掉 `$schema`、`title`，否则部分厂商 API 直接 400 |
| `CallToolResult` | 块列表 → 字符串；图片/资源只回报占位，不把 base64 塞进上下文 |

真正难的只有一处：**MCP SDK 是 async，`BaseTool.call()` 是 sync**。做法是后台线程跑一个常驻协程，主线程通过队列投递请求并同步等结果。注意不能「在主线程建连接、在后台线程用」——anyio 的 task group 进入与退出必须在同一个 task 里。

> **这一条本身就是「接口设计是否合格」的证明**：第三方能力接进来，`core/agent.py` 和 `core/tool.py` 一行没改。

---

## 复盘：自己发现并修掉的 7 个洞

写完第一版之后按生产标准复盘，发现 7 处真问题。**这些不是设计时就想到的**，是回头读代码时才发现的——每一处都补了回归测试钉住：

| 洞 | 后果 | 修法 | 回归测试 |
|---|---|---|---|
| 沙箱只读 `open()` 可读任意绝对路径 | 模型能把 `.env` 的 API key 读进上下文 | 禁绝对路径 + 限定工作目录内，非字面量 fail closed | +7 |
| 预算只统计输出 token | 长上下文场景成本等于没管 | 订阅 `LLM_START` 记输入 prompt，输入 + 输出一起入账 | +2 |
| 上下文截断假设最后一条是 user | 实际常是 function，截断会切错位置 | 从后往前找真 user，工具调用尾巴整段保留 | +6 |
| `file_tools` 的 `root / 绝对路径` 吞掉 root | 模型能读写工作目录之外的文件 | resolve 后 `is_relative_to(root)` 校验 | +7 |
| checkpoint 用 `tool_calls.step` 单列主键 | 一步多工具时 IntegrityError | 改 `(step, seq)` 复合主键，旧库自动迁移 | +4 |
| 指数退避没有 jitter | 多 worker 会同步重试（惊群） | 加可注入 jitter，等待落在 `[wait, 2×wait)` | +3 |
| docstring 里残留备课痕迹 | 面试官翻到会觉得「这代码不是他写的」 | 清理 + 静态检查钉住 | +3 |

> 这 7 条比「我设计了一个完美系统」可信得多。**真实工程是写完发现漏了、补上、钉住。**

---

## 架构总览

```mermaid
flowchart TB
    subgraph 入口
        CLI[cli.py<br/>run / replay]
    end

    subgraph 核心
        LOOP[Agent 主循环<br/>think → act → observe]
        REG[工具注册表<br/>TOOL_REGISTRY + tool_map]
    end

    subgraph 能力层
        LLM[LLM 适配器<br/>OpenAI 兼容直连]
        TOOLS[工具<br/>SQL / 文件 / 沙箱]
        MEM[记忆<br/>短期窗口 / RAG]
        MA[多智能体<br/>串行 · 并行]
    end

    subgraph 横切关注点（事件订阅者）
        TRACER[tracer]
        BUDGET[budget]
        RETRY[retry]
        APPROVAL[approval]
        ROLLBACK[rollback]
    end

    CLI --> LOOP
    LOOP --> LLM
    LOOP --> TOOLS
    LOOP --> MEM
    CLI --> MA
    MA --> LOOP
    TRACER -.事件流.-> LOOP
    BUDGET -.事件流.-> LOOP
    RETRY -.事件流.-> LOOP
    APPROVAL -.事件流.-> LOOP
    ROLLBACK -.事件流.-> LOOP
    TRACER --> TRACE[(trace.jsonl<br/>可回放)]
```

**一条主线架构决策**：这些能力**没有一条写死在主循环里**，全部通过「事件总线」挂载为订阅者 —— 加一个能力 = 加一个订阅者，主循环几乎不用改。

---

## 快速开始

### 运行前提

**可以独立 clone 运行**，不依赖 `qwen_agent` 包。核心只需要 Python 3.10+ 和 `pytest`：

```bash
git clone https://github.com/DFY04/miniagent
cd miniagent
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

可选依赖（不装也能跑，相关测试会自动跳过）：`mcp`（MCP 接入测试）、`matplotlib`（沙箱产图测试）、`chromadb` + `dashscope`（真实 RAG）。

### 跑测试（不需要 API key、不联网）

```bash
python -m pytest miniagent -q
```

### 看三个离线 demo

```bash
python -m miniagent.multiagent.parallel_demo   # 并行编排：时间条会重叠，加速比 ≈3
python -m miniagent.tools.sandbox              # 沙箱：8 组真机输出（拒绝/超时/产图）
python -m miniagent.mcp.demo                   # MCP 接入：定义 → 挂载 → 调用（起本地子进程）
```

### 跑真 Agent（需要 `.env` 里的 `DASHSCOPE_API_KEY`）

```bash
python -m miniagent.cli --help
python -m miniagent.cli run "你的问题"
python -m miniagent.cli run "文档该怎么切分" --rag miniagent/memory/asn.txt --trace trace.jsonl
python -m miniagent.cli replay trace.jsonl

# 换供应商（key 放 .env：DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）
python -m miniagent.cli run "你的问题" --provider deepseek
```

> 换供应商只换一个 provider 参数，`llm.py` 内部把它展开成对应的 OpenAI 兼容端点（DeepSeek 走 `deepseek-chat`）。
> ⚠️ DeepSeek 必须用 `deepseek-chat`——`deepseek-reasoner`(R1) 不支持 function calling，而本项目的 Agent 靠它驱动工具。

---

## 测试：314 条，全部离线

不调真实 LLM、不烧 API、不需要 key（用假 LLM + 假执行器；MCP 那组只起本地子进程）。`main` 分支的 CI 在 `ubuntu-latest` + Python 3.10/3.12 上跑全套。

| 测试文件 | 覆盖 | 条数 |
|---|---|---|
| `test_m1_offline.py` | 主循环 + 工具注册表 | 8 |
| `test_p01_hooks.py` | 事件总线 | 6 |
| `test_p02_context_truncate.py` | 上下文截断（最后一条是 tool/function 的边界） | 6 |
| `test_p05_tracer.py` | 事件落盘 + 回放 | 14 |
| `test_p06_budget.py` | 预算（输入 + 输出 token）与 off-by-one 边界 | 18 |
| `test_p07_retry.py` | 重试（含 jitter）+ 死循环检测 | 23 |
| `test_p09_approval.py` | 审批闸门 | 17 |
| `test_p10_checkpoint.py` | 存档 / 回滚 / 一步多工具 | 24 |
| `test_p10_file_tools.py` | 文件工具越界防护 | 7 |
| `test_p11_sandbox.py` | 沙箱三层防护（含绝对路径拦截） | 34 |
| `test_p14_orchestrator.py` | 串行编排（身份转换 / 分段合并） | 34 |
| `test_p15_parallel.py` | 并行编排（真并行 / 并发上限 / 部分失败） | 27 |
| `test_p16_parallel_writes.py` | 并行写安全（冲突须在执前拦 / 快照早于写入） | 20 |
| `test_p17_mcp.py` | MCP 接入（schema 清洗 / 挂载 / 端到端调用 / 生命周期） | 16 |
| `test_p18_llm_provider.py` | 供应商切换（cfg 形状 / key 来源 / 缺 key 早报错） | 11 |
| `test_vector_store.py` | RAG 原语（分块 / 余弦 / 内存向量库） | 14 |
| `test_docs_hygiene.py` | 文档卫生（禁止遗留备课痕迹） | 3 |
| `p02` `p03` `p04` `checkpoint` 内置 | 截断 / 摘要 / 记忆 / 存档 | 32 |
| **合计** | | **314** |

其中几条是「看起来对但其实错」的回归测试，比如：

- **预算 off-by-one**：`max_steps=2` 必须正好跑 2 步，不能跑 3 步
- **快照必须早于写入**：断言影子目录里是**旧内容**（存成「改后」版本等于回滚失效）
- **真并行**：不写死毫秒数，而是同一份计划跑串行/并行两遍比墙钟（避免 flaky）
- **架构约束**：断言串行与并行两个模块**互相不 import**
- **MCP 挂载**：真起一个 MCP server 子进程，断言工具真的进了 `tool_map` 且被调通

---

## 评测：端到端 harness

| 层 | 状态 | 测什么 |
|---|---|---|
| 端到端 | ✅ 已实现（`eval/harness.py`，393 行） | 最后答案对不对：accuracy / 步数 / token / p50·p95 / 失败分类 |
| 轨迹（组件 + 工具调用） | ⏳ 规划中 | 工具选对没、参数对不对、顺序对不对、有没有多余调用；pass@k / pass^k |

为什么两层都要：端到端只告诉你「答错了」，轨迹层才告诉你「错在选工具、抽参数还是排序」。同一批任务过两层，才能把「碰巧答对」和「真的会用工具」分开。

**当前状态说明（不夸大）**：

- 端到端 harness 已实现：准确率、平均步数、平均 token、p50/p95 延迟、失败分类（`wrong_answer` / `empty_answer` / `exception`），并生成 markdown 报告。
- ⚠️ 仓库里的 `eval/report.md` 是**假 LLM demo 跑出来的示例**（`eval/demo.py` 内置的确定性假模型，用于验证 harness 本身），**不是任何真实模型的成绩**。
- 轨迹层**尚未接入真实模型**，也没有可复现的实验数据。

> harness 是「能测」，不等于「测出了好成绩」。真实模型的评测报告会在接上适配器后补，补之前不放任何数字。

---

## 目录结构

```
miniagent/
  cli.py                统一入口（run / replay）
  llm.py                LLM 适配器：OpenAI 兼容接口直连（urllib），含重试与 tool_call 解析
  core/
    message.py          Message / FunctionCall
    tool.py             ★ 两级注册表 + @tool + BaseTool
    agent.py            ★ 主循环 think → act → observe
    hooks.py            事件类型 + 控制流信号
    context.py          token 估算 + 上下文截断
    protocols.py        Protocol 类型（给 mypy）
    mem_agent.py        带记忆 + 存档的主循环
  runtime/              横切关注点（全部通过事件总线挂载）
    tracer.py  budget.py  retry.py  approval.py  rollback.py
  checkpoint/           快照存档 + 回滚 demo
  memory/               短期记忆 / embedding（离线玩具 + DashScope）/ 向量库
  tools/                SQLite 查询 / 文件 / 轻量沙箱
  multiagent/           成员名册基类 + 串行 handoff + 并行 fan-out
  mcp/                  MCP 接入（客户端 + 适配器 + 测试用 server）
  tests/                离线验收测试
```

---

## 项目来源与边界

这个项目不是凭空设计的，也不是照抄的。把来源说清楚，是对读者的尊重，也避免高估它。

**来自课程/参考实现的部分**
- 整体架构蓝图（主循环 / 工具注册表 / 记忆 / 多智能体 / MCP 的分层方式）
- 各阶段的验收标准与测试用例设计
- 少量模块直接采用了课程提供的参考实现：`runtime/tracer.py` 与 `runtime/tracer_reference.py` 内容一致

**自己实现的部分**
- 除上述参考实现外，各模块代码均逐模块实现（git 历史 40 次提交 / 13 天，可见「写练习稿 → 失败 → 参考 → 再实现」的过程）
- 自己做的改造与踩坑：
  - **主循环**：显式 `max_steps` 参数化、工具异常转 observation 回灌模型
  - **并行编排**（`multiagent/parallel.py`，806 行）：写冲突在派活前检测、动手前快照，执行器可注入
  - **checkpoint**：对话上下文与磁盘文件用同一个 step 号同步回滚，`declare_writes` 声明式快照
  - **MCP 接入**：后台线程常驻事件循环 + Future 桥接 async SDK 与同步接口
  - **沙箱**：AST 静态检查 + 子进程超时 kill + 工作目录隔离三层防护
- 踩过的坑与取舍记录在各模块的中文注释里

> 如果把它当作品评估：**实现是完整的，设计不全是原创的。**
> 建议优先读 `multiagent/parallel.py`、`checkpoint/checkpoint.py`、`tools/sandbox.py` 这三个改造最多的模块。

---

## 诚实的边界

这个项目有很多"没做"，写出来是为了不被追问时露怯：

- **沙箱不是真隔离**：静态检查拦不住 `__import__("o"+"s")` 这类运行时拼名字的绕过，防的是"手滑"不是"恶意"。
- **LLM 层是自写的简化版**：只做 OpenAI 兼容的 chat + 单 tool_call 解析，**不支持一次回复多个 tool_calls**，
  也不处理各家方言差异（如 DashScope 原生 API 与兼容模式的差别），无流式。
- **评测只到「能测」**：端到端 harness 已实现并有失败分类，但**没有真实模型的评测结果**；轨迹层未接真模型。
  测试测确定性、评测测统计性，这两件事我分得清。
- **多智能体没接预算/追踪**：`budget.py`、`tracer.py` 尚未挂到 `multiagent/` 上，多轮/多并发的成本仍不受控。
- **并行收益有上限**：瓶颈在 API 延迟与限流；同一个成员默认不并发（防数据竞争）。
  离线 demo 的加速比是注入固定延迟测的，**不是真实 API 的加速比**。
- **没有真实用户**：这是个离线个人项目，没有上线、没有 QPS、没有灰度。缺的正好是生产流量这一课。

---

## 里程碑

沿 `M1 → M7` 一条线推进，每个里程碑都有独立验收测试：

| | 内容 | 状态 |
|---|---|---|
| M1 | 主循环 + 工具注册表 + LLM 适配器 | ✅ |
| M2 | 上下文截断 + 摘要压缩 + 短期记忆 + RAG | ✅ |
| M3 | Tracer（落盘 + 回放） | ✅ |
| M4 | 预算 / 重试 / 死循环检测 | ✅ |
| M5 | 审批闸门 + checkpoint/回滚 + 沙箱 | ✅ |
| M6 | 评测 harness | ✅（端到端已实现；轨迹层待接） |
| M7 | 多智能体编排（串行 + 并行） | ✅ |

---

## 与 qwen-agent 的对应关系

| MiniAgent | qwen-agent |
|---|---|
| `core/tool.py` `TOOL_REGISTRY` / `@tool` | `tools/base.py` `TOOL_REGISTRY` / `register_tool` |
| `core/agent.py` `Agent.run()` | `agents/fncall_agent.py` `FnCallAgent._run` |
| `core/message.py` `Message` | `llm/schema.py` `Message` |
| `llm.py` `LLMClient` | `llm/base.py` `BaseChatModel`（本项目改为 OpenAI 兼容接口直连，不依赖 SDK） |
| `tools/sandbox.py` `PythonSandbox` | `tools/code_interpreter.py`（真容器） |
| `multiagent/hub.py` `MultiAgentHub` | `multi_agent_hub.py` `MultiAgentHub`（本项目把身份转换也提到了基类） |
