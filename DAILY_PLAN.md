# MiniAgent 施工计划（每日）

> 配合 `BLUEPRINT.md` 使用。蓝图讲"最终要做成什么"，本文件讲"每天具体干什么"。
>
> **协作模式（已约定）**：我给 **spec + 骨架（函数签名 + TODO）+ 离线测试**，**核心逻辑你自己写**；红了你自己查；卡死 30 分钟再问我，我只给提示。

---

## 每日固定流程

| 步骤 | 谁做 | 内容 |
|------|------|------|
| 1 | 我 | 交当天 spec + 骨架文件（含 TODO）+ 离线测试 |
| 2 | 你 | 写核心逻辑（填 TODO） |
| 3 | 你 | 跑离线测试：`python -m miniagent.tests.test_xx` |
| 4 | 你 | 红了 → 自己 debug；≥30 分钟无进展 → 问我（只要提示） |
| 5 | 我 | review 代码，指出设计问题（不看对错，看设计） |
| 6 | 我 | `git commit`（一天一个 commit，message 说清做了什么） |

## 通用规则

- **验收 = 离线测试全绿**。测试跑不过就是没做完，不许"看着像对的"。
- **禁止烧 API 调试**：日常开发用离线假 LLM，只有当天验收才跑一次真实 demo。
- **一天一个 commit**，commit message 格式：`feat(m2): context truncate by token budget`。
- **写不出来先写注释**：把"我打算怎么做"写成中文注释再填代码，我们会看注释 review 你的思路。

## 卡住时的求助格式（重要）

```
【卡在哪】函数名 + 行号
【现象】报错信息 / 或"结果不对：期望 X，实际 Y"
【我试过】你排查的 2~3 步
【我的猜测】你觉得问题可能在哪
```

> 带这四项来问，我只回提示；不带这四项，说明你还没真卡住。

---

## 16 天总览（含 M1 补课）

| 天 | 里程碑 | 主题 | 交付 | 预计 |
|----|--------|------|------|------|
| **M1** | — | **骨架（⚠️ AI 代写）** | core / llm / tools / tests | ✅ 已完成 |
| **P00** | M1 补课 | **重写核心，把 M1 变成你自己的** | 你重写 `core/tool.py` `core/agent.py` | 4h |
| P01 | M2 记忆 | 事件总线 hooks | `core/hooks.py` | 3h |
| P02 | M2 | 上下文截断 | `core/context.py::truncate` | 3h |
| P03 | M2 | 摘要压缩 | `core/context.py::summarize` | 4h |
| P04 | M2 | 短期 + 长期记忆 | `memory/short_term.py` `memory/long_term.py` | 4h |
| P05 | M3 可观测 | Tracer + 回放 | `runtime/tracer.py` | 4h |
| P06 | M4 可靠 | 预算 | `runtime/budget.py` | 3h |
| P07 | M4 | 重试 + 死循环检测 | `runtime/retry.py` | 4h |
| P08 | M4 | 统一 CLI | `cli.py` | 3h |
| P09 | M5 可控 | 审批闸门 | `runtime/approval.py` | 4h |
| P10 | M5 | Checkpoint / Resume | `runtime/checkpoint.py` | 4h |
| P11 | M5 | 轻量沙箱 | `tools/sandbox.py` | 4h |
| P12 | M6 评测 | 评测 harness | `eval/harness.py` + `eval/cases/` | 4h |
| P13 | M6 | 扩用例 + 调优 | `eval/report.md` | 3h |
| P14 | M7 编排 | 多智能体 orchestrator | `multiagent/orchestrator.py` | 4h |
| P15 | 收尾 | README + 面试演练 | `README.md` | 3h |

---

# M1 · 骨架（已完成 · ⚠️ AI 代写）

## Day M1 — 已交付内容（你还没读）

> 这部分是我写的，不是你写的。**它是参考实现，不是你的成果。**

| 文件 | 内容 | 行数 |
|------|------|------|
| `core/message.py` | Message / FunctionCall | 64 |
| **`core/tool.py`** | ★ 两级注册表：`TOOL_REGISTRY` + `@tool` + `BaseTool` | 79 |
| **`core/agent.py`** | ★ 主循环 `Agent.run()` | 97 |
| `llm.py` | LLM 适配器（包装 qwen-agent） | 56 |
| `tools/data_tools.py` | list_tables / sql_query | 87 |
| `tests/test_m1_offline.py` | 假 LLM 离线测试（全绿） | 105 |

**M1 导读（只要记住这两个文件是核心）**：
- `core/tool.py` —— 全局 `TOOL_REGISTRY` 存「类」，`Agent.tool_map` 存「实例」；`@tool` 负责第一级注册。
- `core/agent.py` —— `run()` 就是 think→act→observe 循环。

---

## Day P00 — M1 补课：把 AI 写的变成你自己的

> M1 · 预计 4h · **整个计划里最重要的一天**

**目标**：M1 是我写的，你不读就等于没有。今天把它变成你的。

**任务（按顺序）**：

1. **读**（1h）：按依赖自底向上读 —— `core/message.py` → `core/tool.py` → `llm.py` → `core/agent.py`。每读完一个文件，**合上**，用自己的话写下「它干了什么、为什么这么设计」。
   （`message.py` 和 `tool.py` 都是零依赖，顺序随意；`agent.py` 依赖前两者、并且调 `llm.chat()`，**必须最后读**。）
2. **重写**（2.5h）：**关掉原文件**，照着 `README.md` + 上面的导读，从零重写：
   - `core/tool.py`（两级注册表 + `@tool` + `BaseTool`）
   - `core/agent.py`（主循环）
   （`message.py` 是纯 dataclass，读一遍即可，不必重写。）
3. **验证**（0.5h）：用我写的 `tests/test_m1_offline.py` 验证你的重写（**测试文件不许改**）。
4. **复述**：脱稿讲清「为什么注册要分两级」。

**我给**：只在你卡住 ≥30 分钟后给提示；最后 review 你的重写，和原版逐行对比。

**验收**：
- [ ] 你重写的 `tool.py` / `agent.py` 能通过 `tests/test_m1_offline.py`
- [ ] 能脱稿解释两级注册表（类 vs 实例）
- [ ] 能脱稿画出主循环

**规则**：重写时允许看 `README.md`（那是 spec），**不允许看原 `tool.py` / `agent.py`**。
区别就在这——spec 可以看，代码必须自己写。

---

# M2 · 记忆层

## Day P01 — 事件总线（hooks）

> M2 · 基础啊 · 预计 3h · 这是后面 M3-M6 全部特性的地基

**目标**：主循环只发事件，运行时特性（trace/预算/审批）作为订阅者挂上去，主循环零侵入。

**文件**：`miniagent/core/hooks.py`

**你要写**：
```python
class EventType(Enum):
    RUN_START, LLM_START, LLM_END, TOOL_START, TOOL_END, STEP_END, RUN_END = ...

@dataclass
class Event:
    type: EventType
    data: dict          # 事件负载（如 {'name': 'sql_query', 'args': ...}）

class EventBus:
    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None: ...
    def emit(self, event: Event) -> None: ...      # 分发给所有订阅者
    def subscribe_all(self, handler) -> None: ...  # 订阅所有事件
```
- 边界：某个订阅者抛异常，**不能影响其他订阅者，也不能中断主循环**（捕获 + 记录）。
- 边界：订阅同一个事件多个 handler，按注册顺序调用。

**我给**：`EventType` 枚举 + `Event` dataclass + `EventBus` 骨架 + `tests/test_p01_hooks.py`

**验收**：
- [ ] 订阅者能按顺序收到事件
- [ ] 一个订阅者抛异常，其他订阅者照常收到

**提示**：内部就是 `dict[EventType, list[Callable]]`；"隔离异常"用 try/except 包住每次 handler 调用。

---

## Day P02 — 上下文截断（truncate）

> M2 · 记忆层 · 预计 3h

**目标**：消息超过 token 预算时，从最老的消息开始裁，但**保留 system 和最后一条 user**。

**文件**：`miniagent/core/context.py`

**你要写**：
```python
def estimate_tokens(text: str) -> int: ...        # 粗略估算
def truncate(messages: list[Message], max_tokens: int) -> list[Message]: ...
```
- `estimate_tokens`：中英混合的粗略估法（别调 tokenizer，够用即可）。
- `truncate` 规则：
  1. `system` 消息**永不裁**；
  2. 最后一条 `user` 消息**永不裁**；
  3. 其余从**最老**开始删，直到总量 ≤ `max_tokens`；
  4. 裁剪后必须仍是合法对话（不能出现孤立的 `function` 消息——它的 `assistant` 被裁了就一起裁）。
- 边界：单条消息就超预算 → 至少要保住 system + 最后 user（允许超）。

**我给**：两个函数签名 + docstring + `tests/test_p02_truncate.py`

**验收**：
- [ ] 超长对话被裁到预算内
- [ ] system 和最后一条 user 一定还在
- [ ] 不出现孤立的 function 消息

**提示**：先把 system 和最后一条 user 拎出来（它们占的额度先扣掉），剩下的从老到新累加填充。

---

## Day P03 — 摘要压缩（summarize）

> M2 · 记忆层 · 预计 4h

**目标**：被裁掉的老消息不要直接丢，压成一条"摘要消息"接在 system 后面。

**文件**：`miniagent/core/context.py`

**你要写**：
```python
def summarize(messages: list[Message], llm) -> str: ...
def compress(messages: list[Message], llm, max_tokens: int) -> list[Message]: ...
```
- `summarize`：把老消息交给 LLM，让它输出一段**保留事实（人名/数字/结论）、丢弃寒暄**的摘要。
- `compress`：`truncate` 的更优版本——超预算时，先摘要把要丢的部分压缩，再看是否还超。
- 边界：LLM 摘要失败 → 降级成 `truncate`（不能因为摘要失败就崩）。

**我给**：签名 + 摘要 prompt 模板 + `tests/test_p03_compress.py`（用假 LLM 返回固定摘要）

**验收**：
- [ ] 压缩后 token 达标
- [ ] 摘要里保留了原对话的关键事实（测试断言含关键词）
- [ ] 假 LLM 抛错时自动降级成 truncate

**提示**：摘要消息建议 role=system 或 user，标明"以下是之前对话的摘要"。

---

## Day P04 — 短期 + 长期记忆

> M2 · 记忆层 · 预计 4h

**目标**：短期记忆（对话窗口）+ 长期记忆（跨会话检索）。

**文件**：`miniagent/memory/short_term.py`、`miniagent/memory/long_term.py`

**你要写**：
```python
# short_term.py
class ShortTermMemory:
    def __init__(self, max_tokens: int): ...
    def add(self, msg: Message) -> None: ...       # 加入并维护窗口
    def build(self) -> list[Message]: ...           # 返回压缩后的消息列表

# long_term.py
class LongTermMemory:
    """复用 qwen-agent 的 retrieval 工具，不重造向量检索。"""
    def add_documents(self, paths: list[str]) -> None: ...
    def retrieve(self, query: str) -> str: ...      # 返回相关片段
```
- `long_term.retrieve` 内部直接调 qwen-agent 的 `retrieval` 工具（`TOOL_REGISTRY` 里那个），你只做包装。
- 接入 Agent：在 `Agent.run` 开头，把 `long_term.retrieve(最后一条 user)` 的结果作为一条 system/user 消息注入。

**我给**：两个类骨架 + `tests/test_p04_memory.py`

**验收**：
- [ ] 短期记忆超过预算会自动压缩
- [ ] 长期记忆能从一个文档里检索出相关片段

**提示**：long_term 里 `from qwen_agent.tools import Retrieval`，实例化后 `.call({'query':..., 'files':...})`。

---

# M3 · 可观测

## Day P05 — Tracer + 回放

> M3 · 可观测层 · 预计 4h

**目标**：每次 LLM/工具调用落成 JSONL，可回放复现。

**文件**：`miniagent/runtime/tracer.py`

**你要写**：
```python
class Tracer:
    def __init__(self, path: str): ...             # trace 存放路径
    def attach(self, bus: EventBus) -> None: ...    # 订阅所有事件
    # 内部：把 Event 加时间戳、trace_id 后写一行 JSON

def replay(path: str) -> None: ...                  # 读 JSONL，打印事件序列
```
- 每条记录至少含：`ts`、`type`、`trace_id`、`data`；LLM_END 要含 `latency_ms`。
- `replay` 把一次 run 的事件按顺序打印出来。

**我给**：类骨架 + `tests/test_p05_tracer.py`

**验收**：
- [ ] 一次 run 生成的 JSONL 行数正确
- [ ] `replay` 能按原顺序还原事件

**提示**：耗时用 `time.perf_counter()` 在 LLM_START/LLM_END 之间算；写入用 `json.dumps` + `\n`。

---

# M4 · 可靠性

## Day P06 — 预算（budget）

> M4 · 运行时层 · 预计 3h

**目标**：token / 成本 / 步数上限，超限就优雅中断。

**文件**：`miniagent/runtime/budget.py`

**你要写**：
```python
class BudgetExceeded(Exception): ...

class Budget:
    def __init__(self, max_steps, max_tokens, max_cost_usd): ...
    def on_llm_end(self, event: Event) -> None: ...   # 累加 usage
    def on_step_end(self, event: Event) -> None: ...  # 步数 +1
    def check(self) -> None: ...                      # 超限抛 BudgetExceeded
```
- 从 hooks 的 `LLM_END` 事件里拿 token 数（`usage`）；成本用一个单价表换算。
- 主循环捕获 `BudgetExceeded`，`on_run_end(reason='budget')`。

**我给**：类骨架 + `tests/test_p06_budget.py`

**验收**：
- [ ] 造一个超预算的 run，被优雅中止并给出 reason
- [ ] 未超限时不误报

---

## Day P07 — 重试 + 死循环检测

> M4 · 运行时层 · 预计 4h

**目标**：LLM 调用失败能重试；agent 反复调"同一个工具+同一组参数"能止损。

**文件**：`miniagent/runtime/retry.py`

**你要写**：
```python
def with_retry(fn, max_retries=3, base_delay=1.0): ...   # 指数退避
class LoopDetector:
    def __init__(self, window=3): ...
    def check(self, tool_name: str, args: str) -> None: ...   # 重复超阈值 → 抛 LoopDetected
```
- 重试：捕获异常 → `base_delay * 2**n` 退避 → 超过 `max_retries` 抛出。
- 死循环：记录最近 N 次 `(tool_name, args)` 的哈希，连续相同次数超阈值就抛。

**我给**：函数/类骨架 + `tests/test_p07_retry.py`

**验收**：
- [ ] 连续失败 N 次后重试成功 / 或按预期抛出
- [ ] 模拟重复调用同一工具同一参数 → 被检出

**提示**：退避用 `time.sleep`；测试里把 sleep 打成 0 免得跑太久。

---

## Day P08 — 统一 CLI

> M4 · 入口 · 预计 3h

**目标**：一个入口跑所有事。

**文件**：`miniagent/cli.py`

**你要写**：
```bash
python -m miniagent.cli run "2023年销售额最高的城市？"   # 跑 agent
python -m miniagent.cli replay <trace.jsonl>              # 回放一次 trace
python -m miniagent.cli eval                              # 跑评测（M6 再接）
```
- 用 `argparse` 做子命令。

**我给**：`argparse` 骨架 + 三个子命令的 TODO

**验收**：
- [ ] 三个子命令都能跑（eval 先占位）
- [ ] `run` 能接真实 LLM 跑通 demo

---

# M5 · 可控性

## Day P09 — 审批闸门（approval）

> M5 · 运行时层 · 预计 4h · ⭐ 面试重点

**目标**：危险工具**执行前**暂停，等人工批准。

**文件**：`miniagent/runtime/approval.py`

**你要写**：
```python
class ApprovalGate:
    def __init__(self, dangerous: list[str], policy=None): ...
    def on_tool_start(self, event: Event) -> None:
        # 命中危险规则 → 打印待执行命令 → input('批准？y/n')
        # 拒绝 → 抛 ApprovalRejected
```
- 默认危险规则：SQL 里出现 `DROP / DELETE / UPDATE / INSERT / ALTER`；命令里出现 `rm / sudo`。
- 非交互模式（CI/测试）：注入一个 `policy` 回调决定自动批准/拒绝。

**我给**：类骨架 + `tests/test_p09_approval.py`（用注入 policy，不阻塞）

**验收**：
- [ ] `DROP TABLE` 被拦
- [ ] `SELECT` 放行
- [ ] 拒绝时主循环收到 `ApprovalRejected` 并优雅结束

**提示**：审批必须在 `tool.call()` **之前**（副作用前）。

---

## Day P10 — Checkpoint / Resume

> M5 · 运行时层 · 预计 4h

**目标**：每步存快照，中断后能接着跑。

**文件**：`miniagent/runtime/checkpoint.py`

**你要写**：
```python
class Checkpointer:
    def __init__(self, path): ...
    def on_step_end(self, event: Event) -> None: ...   # 存 {step, messages, budget} 快照
    def load(self) -> dict | None: ...
def resume(path, agent) -> Iterator[Message]: ...      # 从快照继续
```
- 快照内容：`messages` 全量 + `step` + `budget` 累计值。
- `resume` 从 `step+1` 继续跑。

**我给**：类骨架 + `tests/test_p10_checkpoint.py`

**验收**：
- [ ] 存了 N 步快照
- [ ] 从第 2 步的快照 resume，能跑完剩余步骤

---

## Day P11 — 轻量沙箱（sandbox）

> M5 · 工具层 · 预计 4h · ⭐ 差异化亮点

**目标**：能跑 Python 画图，**不依赖 Docker**（qwen-agent 的 `code_interpreter` 要 Docker，这是对比点）。

**文件**：`miniagent/tools/sandbox.py`

**你要写**：
```python
@tool('python_sandbox')
class PythonSandbox(BaseTool):
    # 在子进程里执行代码，超时 kill，禁危险 import（os/subprocess/socket...）
    def call(self, params, **kwargs) -> str: ...
```
- 用 `subprocess.run(..., timeout=N)` 执行临时脚本。
- 静态检查：源码里出现禁用的 import / `open(...,'w')` 就拒绝。
- 产出图片路径要返回给调用者。

**我给**：类骨架 + 禁名单 + `tests/test_p11_sandbox.py`

**验收**：
- [ ] 正常画图代码能产出 PNG
- [ ] `import os` / 超时 / 死循环 被拒或被 kill

**提示**：`subprocess.run([sys.executable, tmpfile], timeout=10, capture_output=True)`。

---

# M6 · 评测

## Day P12 — 评测 harness

> M6 · 评测层 · 预计 4h · ⭐ 面试最值钱

**目标**：跑用例 → 打分 → 出报告。

**文件**：`miniagent/eval/harness.py`、`miniagent/eval/cases/*.jsonl`

**你要写**：
```python
def load_cases(path) -> list[dict]: ...          # 读 jsonl
def score(answer: str, expected, scorer: str) -> bool: ...   # contains/numeric/set_match
def run_eval(agent_factory, cases) -> dict: ...  # 跑全部用例，收集指标
def write_report(result, path) -> None: ...      # 生成 report.md
```
- 指标：准确率 / 平均步数 / 平均 token / p50·p95 延迟 / 失败分类。

**我给**：骨架 + 10 条示例用例 + `tests/test_p12_eval.py`

**验收**：
- [ ] 10 条用例跑出准确率
- [ ] 生成 `eval/report.md`

---

## Day P13 — 扩用例 + 调优

> M6 · 评测层 · 预计 3h

**目标**：用例扩到 50 条，跑基线，分析失败。

**你要写**：
- 补用例到 50 条（变体：不同年份/聚合/排序/多表）
- 读报告，定位失败类型

**我给**：用例模板 + 一个"失败分析"清单

**验收**：
- [ ] 50 条跑完，report.md 有完整指标
- [ ] 给出至少 2 条改进（改 system prompt / 改工具 description）并复测

---

# M7 · 多智能体

## Day P14 — Orchestrator

> M7 · 编排层 · 预计 4h

**目标**：多个 Agent 协作完成一个任务。

**文件**：`miniagent/multiagent/orchestrator.py`

**你要写**：
```python
class Orchestrator:
    def __init__(self, agents: dict[str, Agent], mode: str = 'sequential'): ...
    def run(self, messages) -> Iterator[Message]:
        # sequential: 依次跑，前一个的输出作为后一个的输入
        # router:     用一个"路由 agent"决定交给谁（参考 qwen-agent 的 Router）
```
- 先做 `sequential`，再加 `router`。

**我给**：类骨架 + `tests/test_p14_orchestrator.py`

**验收**：
- [ ] sequential 模式：分析师 → 批判者 → 总结者 能串起来
- [ ] router 模式：能按问题类型选对 agent

---

# 收尾

## Day P15 — README + 面试演练

> 收尾 · 预计 3h

**目标**：把 BLUEPRINT 变成"实测版" README。

**你要写**：
- 更新 `README.md`：架构图 + 真实评测数据（准确率/成本/延迟）+ 设计取舍
- 写一段 3 分钟面试讲稿并录音复述

**验收**：
- [ ] README 里有可复现的评测数字
- [ ] 能脱稿讲清 Hooks 设计、审批闸门、评测方法

---

## 进度追踪

- [x] M1 骨架（AI 代写）
- [ ] **P00 M1 补课（重写核心）** ← 最重要
- [ ] P01 hooks
- [ ] P02 truncate
- [ ] P03 summarize
- [ ] P04 memory
- [ ] P05 tracer
- [ ] P06 budget
- [ ] P07 retry
- [ ] P08 cli
- [ ] P09 approval
- [ ] P10 checkpoint
- [ ] P11 sandbox
- [ ] P12 eval harness
- [ ] P13 扩用例 + 调优
- [ ] P14 orchestrator
- [ ] P15 README

---

## 备注：与蓝图的微调

`BLUEPRINT.md` 里把 `hooks.py` 标在 M2，本计划把它放在 **M2 第一天（P01）**——因为后面 M3~M6 全部特性都订阅它，先把它立起来最省事。其余对应关系不变。
