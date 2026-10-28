# MiniAgent

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-314%20passed-brightgreen)
![Dependencies](https://img.shields.io/badge/runtime%20deps-1-lightgrey)
![Offline](https://img.shields.io/badge/tests-offline%20only-informational)

> 参照 [qwen-agent](https://github.com/QwenLM/Qwen-Agent) 的架构范式，从零实现 Agent 的**骨架**与**生产级可靠性设施**。
> 主循环、工具系统、记忆、可观测性、安全、可靠性、多智能体编排全部自己实现；LLM 层是 OpenAI 兼容接口的直连实现（urllib，无第三方 SDK）。

**87 个 Python 文件 · 11,078 行 · 314 条离线测试全绿 · 不依赖 Docker · 不联网可跑**

> 关于「这个项目是怎么来的」：整体架构蓝图与阶段验收标准来自课程与官方实现，
> 代码是本人逐模块实现的，**并在若干处做了自己的改造与踩坑**（见 [项目来源与边界](#项目来源与边界)）。

---

## 30 秒看懂

大多数人写 Agent 项目，都在写"怎么调模型"。这个项目反过来：**假设模型调用已经有了，专门把 Agent 在生产里会咬人的那些问题一个个补上。**

```
工具报错        → 工具永不抛异常，失败包装成 observation 回给模型
模型死循环      → max_steps + 连续重复调用检测 + token/步数预算
删库/写文件     → 审批闸门（人工确认）+ 轻量沙箱 + checkpoint 回滚
出了 bug 查不了 → 事件总线 + Tracer 落 JSONL，可回放
多轮撑爆窗口    → 上下文截断 + 摘要压缩 + 短期记忆
LLM 生成的代码  → 子进程沙箱，三层防护
第三方工具接入  → MCP 适配器（把 MCP server 的工具挂成普通工具）
```

**一条主线架构决策**：这些能力**没有一条写死在主循环里**，全部通过「事件总线」挂载为订阅者 —— 加一个能力 = 加一个订阅者，主循环几乎不用改。

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

- 工具用 `declare_writes(params)` **声明**"我这次会改哪些文件"，Agent 在动手**前**存档
- 工具不需要认识 Checkpoint（解耦）；回滚时把消息列表截断 + 文件从影子目录恢复
- 步号由 Agent 的循环变量显式驱动（`begin_step`），保证"上下文第几步"和"文件第几步"永远对齐

### 5. 轻量沙箱：三层防护，各管一件互不覆盖的事

| 层 | 防什么 | 机制 |
|---|---|---|
| ① 静态检查（执行前） | 碰危险**东西** | AST 遍历，拦危险 import / 写方式 `open()` |
| ② 超时 kill（执行中） | 永远**不结束** | `subprocess.run(timeout=N)` |
| ③ 目录隔离（执行后） | 写到**哪儿** | `cwd=临时目录` + 扫目录收产出 |

- 行 `while True: pass` 一个危险 import 都没有 → 只有 ② 能拦
- 行 `import os; os.remove(...)` 一毫秒跑完 → 只有 ① 能拦
- 行 `plt.savefig('C:/x.png')` 合法且瞬间 → 只有 ③ 能拦

**诚实边界**：静态检查是"提高门槛"，不是安全边界。真隔离要容器（qwen-agent 的 `code_interpreter` 走的就是那条路）。

### 6. 多智能体：两种编排策略是**兄弟**，不是继承链

`Orchestrator`（串行/共享上下文）与 `FanOutOrchestrator`（并行/上下文隔离）都继承 `MultiAgentHub`，但**互相不 import**，且有测试盯着这条约束。

| | 串行 `Orchestrator` | 并行 `FanOutOrchestrator` |
|---|---|---|
| 谁决定下一步 | LLM 每轮选人（handoff） | planner 一次拆完，执行器并发 |
| 上下文 | **共享**一条时间线 | **隔离**，每人只见自己的子任务 |
| 核心难题 | 身份转换：别人的话算 user 还是 assistant | 结果归位 / 并发上限 / 部分失败 / 写冲突 |
| 适合 | 辩论、评审、接力 | 批量处理、多路调研 |

两条路线的分水岭：**共享上下文换来「互相看见」，代价是「身份混乱」；隔离换来「简单干净」，代价是「看不见彼此」。**

并行侧额外做了三件"副作用"护栏：**派活前查写冲突**（跑起来再发现就晚了）、**动手前快照**（否则存的是改后版本）、**执行器可注入**（测试用同步执行器，不起线程、结果确定）。

### 7. MCP 接入：只加了一个适配器，核心零改动

`mcp/` 把任意 MCP server（stdio）的工具变成普通 `BaseTool`，`tools=mcp.tools` 就能挂：

| 要翻译的 | 怎么翻 |
|---|---|
| 工具名 | 净化成 `^[a-zA-Z0-9_-]{1,64}$`（可加 `prefix` 防多 server 重名） |
| `input_schema` | 只留 `type / properties / required` —— 丢掉 `$schema`、`title`，否则部分厂商 API 直接 400 |
| `CallToolResult` | 块列表 → 字符串；图片/资源只回报占位，不把 base64 塞进上下文 |

真正难的只有一处：**MCP SDK 是 async，`BaseTool.call()` 是 sync**。做法是后台线程跑一个常驻协程，主线程通过队列投递请求并同步等结果。注意不能"在主线程建连接、在后台线程用"——anyio 的 task group 进入与退出必须在同一个 task 里。

> **这一条本身就是"接口设计是否合格"的证明**：第三方能力接进来，`core/agent.py` 和 `core/tool.py` 一行没改。

---

## 快速开始

### 运行前提

**可以独立 clone 运行**，不依赖 `qwen_agent` 包。只需要 Python 3.10+ 和 `pytest`：

```bash
git clone https://github.com/DFY04/miniagent
cd miniagent
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

用到 MCP 功能时额外装：

```bash
pip install mcp
```

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

### 跑真 Agent（需要 `DASHSCOPE_API_KEY`，写在项目根 `.env`）

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

不调真实 LLM、不烧 API、不需要 key（用假 LLM + 假执行器；MCP 那组只起本地子进程）。

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

其中几条是"看起来对但其实错"的回归测试，比如：

- **预算 off-by-one**：`max_steps=2` 必须正好跑 2 步，不能跑 3 步
- **快照必须早于写入**：断言影子目录里是**旧内容**（存成"改后"版本等于回滚失效）
- **真并行**：不写死毫秒数，而是同一份计划跑串行/并行两遍比墙钟（避免 flaky）
- **架构约束**：断言串行与并行两个模块**互相不 import**
- **MCP 挂载**：真起一个 MCP server 子进程，断言工具真的进了 `tool_map` 且被调通

### 复盘时补的 7 个洞（都带回归测试）

写完第一版后按生产标准复盘，发现 7 处真问题。这些是**先修再写测试钉住**的，不是设计时就想到的：

| 洞 | 修法 | 回归测试 |
|---|---|---|
| 沙箱只读 `open()` 可读任意绝对路径（能把 `.env` 读进上下文） | 禁绝对路径 + 限定工作目录内，非字面量 fail closed | `test_p11_sandbox` +7 |
| 预算只统计输出 token（长上下文的输入完全没算） | 订阅 `LLM_START` 记输入 prompt，输入 + 输出一起入账 | `test_p06_budget` +2 |
| 上下文截断假设最后一条是 user（实际常是 function） | 从后往前找真 user，工具调用尾巴整段保留 | `test_p02_context_truncate` +6 |
| `file_tools` 的 `root / 绝对路径` 会吞掉 root（能读写目录外） | resolve 后 `is_relative_to(root)` 校验 | `test_p10_file_tools` +7 |
| checkpoint 用 `tool_calls.step` 单列主键，一步多工具会 IntegrityError | 改 `(step, seq)` 复合主键，旧库自动迁移 | `test_p10_checkpoint` +4 |
| 指数退避没有 jitter（多 worker 会同步重试） | 加可注入 jitter，等待落在 `[wait, 2×wait)` | `test_p07_retry` +3 |
| docstring 里残留备课痕迹 | 清理 + 静态检查钉住 | `test_docs_hygiene` +3 |

---

## 评测：两层 harness（P12 端到端 + 外部轨迹 harness）

| 层 | 在哪 | 测什么 |
|---|---|---|
| 端到端 | `eval/harness.py`（P12） | 最后答案对不对：accuracy / 步数 / token / p50·p95 / 失败分类 |
| 轨迹（组件 + 工具调用） | 规划中（需要外部 task-level harness） | 工具选对没、参数对不对、顺序对不对、有没有多余调用；pass@k / pass^k |

为什么两层都要：端到端只告诉你「答错了」，轨迹层才告诉你「错在选工具、抽参数还是排序」。
同一批任务过两层，才能把「碰巧答对」和「真的会用工具」分开。

**当前状态：端到端 harness 已实现并可用；轨迹层（接外部 lab1 harness）仍待完成。**

- 端到端 harness（`eval/harness.py` + `eval/demo.py`）已实现：准确率、平均步数、平均 token、p50/p95 延迟、失败分类（`wrong_answer` / `empty_answer` / `exception`），并生成 markdown 报告。
- ⚠️ `eval/report.md` 是**假 LLM demo 跑出来的示例**（`eval/demo.py` 内置的确定性假模型，用于验证 harness 本身），**不是任何真实模型的成绩**。
- 轨迹层（工具选对没 / 参数对不对 / 顺序对不对 / 多余调用）的设计在 `eval/harness.py` 的检查项里，但**尚未接入真实模型跑出结果**，也没有可复现的实验数据。

> 写这一段是为了不夸大：harness 是「能测」，不等于「测出了好成绩」。
> 真实模型的评测报告会在接上适配器后补，补之前不放任何数字。

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
- **评测只到「能测」**：端到端 harness（`eval/harness.py`）已实现并有失败分类，
  但**没有真实模型的评测结果**（`eval/report.md` 是假 LLM demo）；轨迹层设计在，未接真模型。
  测试测确定性、评测测统计性，这两件事我分得清。
- **多智能体没接预算/追踪**：`budget.py`、`tracer.py` 尚未挂到 `multiagent/` 上，多轮/多并发的成本仍不受控。
- **并行收益有上限**：瓶颈在 API 延迟与限流；同一个成员默认不并发（防数据竞争）。
  离线 demo 的加速比是注入固定延迟测的，**不是真实 API 的加速比**。

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
