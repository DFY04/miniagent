"""P15 · FanOutOrchestrator —— 并行编排（fan-out / fan-in）。

════════════════════════════════════════════════════════════════════
【一句话定义】

    把一个【大任务】拆成 N 个【互不依赖】的子任务，同时派给 N 个成员，
    再把 N 个结果汇总成一个答案。

        大任务 ──拆分──▶ [子1, 子2, 子3]
                            │   │   │
                            ▼   ▼   ▼     ← 这一段同时发生（fan-out 扇出）
                          成员A 成员B 成员C
                            │   │   │
                            └───┼───┘
                                ▼         ← 这一段收拢（fan-in 扇入）
                            汇总成最终答案

    三个角色，全部由外部注入（这就是低耦合的落点）：

        planner      —— 拆分：把大任务拆成子任务列表（一个 Agent，输出 JSON）
        成员(agents) —— 干活：普通 Agent，直接当 worker 用
        synthesizer  —— 汇总：把 N 个结果合成一个答案（一个 Agent；不传就直接拼接）

════════════════════════════════════════════════════════════════════
【★ 和 Orchestrator（串行）是什么关系？】

    ┌──────────────────────────────────────────────────────────────┐
    │ MultiAgentHub           成员名册（谁在这个群里）                │  hub.py
    ├────────────────────────────┬─────────────────────────────────┤
    │ Orchestrator               │ FanOutOrchestrator              │
    │ 串行 · 对话式 · 共享上下文   │ 并行 · 隔离 · 汇总               │
    │ 每轮由裁判选人（handoff）    │ 先拆后并（fan-out / fan-in）     │
    │ 要用 _render_view 做身份转换 │ 不用 _render_view（上下文隔离）  │
    │ 适合：辩论 / 评审 / 接力      │ 适合：批量处理 / 多路调研        │
    └────────────────────────────┴─────────────────────────────────┘
                ↑ 两个策略是【兄弟】：互相不 import，换策略只换一个类

    ★ 为什么并行不需要 _render_view？
        串行模式里所有成员共享一条时间线，所以要回答"别人的话对我算 user 还是
        assistant"这个身份问题；并行模式下每个 worker 只拿到【自己那条子任务】，
        别人的话它根本看不到 —— 身份问题不存在。

        这就是那句老话的两面：
            共享上下文 → 换来"互相看见"，代价是"身份混乱"
            上下文隔离 → 换来"简单干净"，代价是"看不见彼此"

════════════════════════════════════════════════════════════════════
【★ 三个可替换的缝（耦合度就靠它们压下来）】

    ① executor —— 把"怎么并发"从"编排逻辑"里剥出去
       默认 ThreadExecutor（线程池）；
       测试注入 SyncExecutor → 顺序确定、不起线程、结果可复现；
       以后想换 asyncio / 多进程，只要再写一个带 map() 的类，本文件一行不改。

    ② planner / synthesizer —— 把"怎么拆、怎么合"交给外部
       编排器只管"按计划派活、把结果收回来"，不关心拆分策略长什么样。

    ③ on_error —— 失败策略
       'collect'（默认）：某个子任务挂了就带着原因记进报告，其它照跑；
       'raise'          ：任何一个挂了就整体抛错。
       ★ 不做"静默降级"：失败原因一定进报告，不做"看起来成功其实少了一块"。

════════════════════════════════════════════════════════════════════
【契约】

    FanOutOrchestrator(
        agents, planner, synthesizer=None, *,
        max_workers=4,          # 同时最多跑几个（防 API 限流）
        executor=None,          # 注入执行器；给了就忽略 max_workers
        max_tasks=16,           # 拆分上限，防 planner 拆出 1000 个任务
        on_error='collect',     # 'collect' | 'raise'
        serialize_per_agent=True,  # 同一个成员不并发（详见下）
        synthesizer_name='synthesizer',
    )

    run(messages) -> Iterator[Message]
        —— 只 yield 最终结果（不吐输入、不吐中间过程）
        —— 跑完可读 self.last_report（每条子任务的成败 / 耗时 / 产出）

    ★ serialize_per_agent=True（默认）
        同一个成员不会被两个子任务【同时】调用（"一个人一次只干一件"），
        不同成员之间照样并行。
        为什么默认开：真实 LLM 客户端 / 有状态的假 LLM 未必线程安全，
        数据竞争是那种"跑一百次才错一次"的最难查的 bug。
        想榨干并发（比如同一个 worker 并行处理 10 个文件）就设 False，
        代价是你要自己保证该 Agent 及其 llm 线程安全。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterator, List, Mapping,
                    Optional, Protocol, Sequence)

from miniagent.core.agent import Agent
from miniagent.core.hooks import ControlFlowSignal
from miniagent.core.message import Message
from miniagent.multiagent.hub import MultiAgentHub


# ============================================================================
#  ① 数据结构：任务 / 结果 / 报告
# ============================================================================
#
#  ★ 为什么每一步都要有结构化的对象，而不是到处传 dict？
#    dict 的 key 写错了不报错（`r['ouput']` 只会 KeyError，甚至加个 .get 就静默
#    变 None）。dataclass 是"有名字、有类型的字段表"，写错属性名当场 AttributeError。
#    而且 IDE / mypy 都能看懂 —— 这是"把约定写进代码"而不是写进注释里。

@dataclass
class SubTask:
    """一个子任务：谁（assignee）去干什么（task）。"""
    id: Any                      # 任务编号（归位用；不保证是 int，planner 给的）
    assignee: str                # 执行者名字，必须在成员名册里
    task: str                    # 要给这个执行者看的那一句话

    def __repr__(self) -> str:
        return f'SubTask(#{self.id} → {self.assignee}: {self.task[:30]!r})'


@dataclass
class TaskResult:
    """一个子任务的执行结果。ok=False 时 error 里是失败原因。"""
    id: Any
    assignee: str
    task: str
    ok: bool
    output: str = ''
    error: str = ''
    elapsed: float = 0.0         # 这条子任务花了多少秒

    def __repr__(self) -> str:
        state = 'ok' if self.ok else f'FAIL({self.error})'
        return f'TaskResult(#{self.id} {self.assignee} {state} {self.elapsed:.3f}s)'


@dataclass
class ParallelReport:
    """整轮并行的体检报告。写完可以直接丢给 tracer / 评测 harness。"""
    results: List[TaskResult] = field(default_factory=list)
    wall_elapsed: float = 0.0    # 墙钟时间：从派活到全部收工

    # ---- 便利视图 ----
    @property
    def ok_results(self) -> List[TaskResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed_results(self) -> List[TaskResult]:
        return [r for r in self.results if not r.ok]

    @property
    def ok_count(self) -> int:
        return len(self.ok_results)

    @property
    def failed_count(self) -> int:
        return len(self.failed_results)

    @property
    def speedup(self) -> float:
        """加速比 = 各任务耗时之和 ÷ 墙钟时间。

            串行时 ≈ 1（甚至略小于 1，因为有调度开销）；
            3 个各 0.1 秒的任务真并行 → 墙钟约 0.1 秒 → 加速比约 3。

        ★ 这是一个"并行到底有没有真发生"的硬指标 —— 光看代码写着线程池
          是不够的，得看这个数。测试里就是靠它证明并行的。
        """
        total = sum(r.elapsed for r in self.results)
        if self.wall_elapsed <= 0:
            return 0.0
        return round(total / self.wall_elapsed, 2)

    def to_dict(self) -> Dict[str, Any]:
        """给 tracer / 报告用的纯数据视图。"""
        return {
            'wall_elapsed': self.wall_elapsed,
            'speedup': self.speedup,
            'ok_count': self.ok_count,
            'failed_count': self.failed_count,
            'tasks': [
                {'id': r.id, 'assignee': r.assignee, 'task': r.task,
                 'ok': r.ok, 'output': r.output, 'error': r.error,
                 'elapsed': r.elapsed}
                for r in self.results
            ],
        }


class PlanError(ValueError):
    """planner 的输出无法变成合法的子任务列表。

    继承 ValueError 而不是 Exception：
    "给你的东西格式不对"在 Python 里就是 ValueError 的语义，
    调用方 `except ValueError` 就能抓住，不用专门记一个新名字。
    """


# ============================================================================
#  ② 执行器：把"怎么并发"从编排逻辑里剥出去
# ============================================================================
#
#  ★ 这是本文件低耦合的核心一招。
#
#    编排逻辑只关心一件事："给我一个函数和一堆任务，把每个任务的结果按原顺序
#    还给我"。至于背后是线程、协程、进程还是干脆串行 —— 它一概不知道。
#
#    这样带来三个好处：
#      1. 测试里注入 SyncExecutor → 没有线程、顺序确定、不会偶发失败（flaky）
#      2. 以后换 asyncio / 多进程 → 新写一个类，FanOutOrchestrator 不动
#      3. 读代码时"编排"和"并发"两件事不会缠在一起
#
#    Protocol 就是"鸭子类型"的正式写法：只要有 map() 就算执行器，不用继承。

class TaskExecutor(Protocol):
    """执行器协议：只有一个方法 map()。"""

    def map(self, fn: Callable[[SubTask], TaskResult],
            tasks: Sequence[SubTask]) -> List[TaskResult]:
        ...


class SyncExecutor:
    """串行执行器（测试 / 调试用）。

    行为契约和线程池版完全一样（顺序、返回值形状都一致），
    只是**不开线程**。所以同一个测试在两种执行器下语义相同 ——
    并行带来的不确定性不会污染"逻辑正确性"的测试。
    """

    def map(self, fn: Callable[[SubTask], TaskResult],
            tasks: Sequence[SubTask]) -> List[TaskResult]:
        return [fn(t) for t in tasks]


class ThreadExecutor:
    """线程池执行器（默认）。

    ★ 为什么线程够用：LLM 调用的瓶颈是"等网络回包"（I/O 等待），
      等待期间 GIL 会被释放，所以多线程能真并行。
      如果是 CPU 密集（比如本地跑大模型推理），才需要多进程。
    """

    def __init__(self, max_workers: int = 4):
        if max_workers < 1:
            raise ValueError(f'max_workers 必须 >= 1，收到 {max_workers}')
        self.max_workers = max_workers

    def map(self, fn: Callable[[SubTask], TaskResult],
            tasks: Sequence[SubTask]) -> List[TaskResult]:
        if not tasks:
            return []
        # pool.map 有两个好性质：
        #   ① 返回结果【按输入顺序】，不用自己归位
        #   ② 内部用异常通道，函数里冒出来的异常会在取结果时抛回主线程
        # ★ 注意这里用 with：块结束时自动 shutdown（等所有线程收工），
        #   不会留下孤儿线程。
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(fn, tasks))


# ============================================================================
#  ③ 拆分结果的解析（独立成函数 → 可以单独测，不用起 Agent）
# ============================================================================

def _extract_json(text: str, raw: str) -> Any:
    """从一段"可能裹着解释文字 / markdown 围栏"的文本里抠出 JSON。

    为什么不能直接 json.loads？
        真实 LLM 很爱输出这种：
            ```json
            [{"id": 1, ...}]
            ```
            好的，我来帮你拆分：
            [{"id": 1, ...}]

    两种常见噪声都要能处理：markdown 围栏、前后解释文字。
    做法：先剥围栏，再"取第一个 [ 到最后一个 ]"（或 {}），再试解析。
    """
    # ① 剥 markdown 围栏：```json ... ``` 或 ``` ... ```
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fence:
        text = fence.group(1).strip()

    # ② 取第一个左括号到最后一个右括号
    for open_ch, close_ch in (('[', ']'), ('{', '}')):
        lo, hi = text.find(open_ch), text.rfind(close_ch)
        if lo != -1 and hi > lo:
            try:
                return json.loads(text[lo:hi + 1])
            except json.JSONDecodeError:
                continue        # 这对括号不行，换另一种试试

    raise PlanError(f'planner 的输出里找不到可解析的 JSON。原文前 200 字：{raw[:200]!r}')


def parse_plan(raw: str) -> List[Dict[str, Any]]:
    """把 planner 的文本输出解析成 [{id, assignee, task}, ...]。

    接受的形状（尽量宽容，因为对面是 LLM）：
        [{"id": 1, "assignee": "分析师", "task": "..."}, ...]
        {"tasks": [ ... 同上 ... ]}

    拒绝的形状（直接 PlanError，不做"猜"）：
        空输出 / 找不到 JSON / 不是列表 / 某条缺 task 或 assignee

    ★ 为什么"缺字段就抛"而不是"给个默认值"？
      因为给默认值等于替 planner 做决定。比如 assignee 默认成第一个人：
      任务被派给了错的人，但程序"看起来成功了" —— 这是最难查的一类 bug。
      宁可当场报错，让人（或上一层的重试）去修 prompt。
    """
    if not raw or not raw.strip():
        raise PlanError('planner 的输出是空的')

    data = _extract_json(raw.strip(), raw)

    # 允许再包一层 {"tasks": [...]}
    if isinstance(data, dict):
        data = data.get('tasks')

    if not isinstance(data, list):
        raise PlanError(f'planner 的输出应该是一个任务列表，实际是 {type(data).__name__}')

    plan: List[Dict[str, Any]] = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise PlanError(f'第 {i} 个子任务不是对象：{item!r}')

        task = str(item.get('task') or '').strip()
        assignee = str(item.get('assignee') or '').strip()

        if not task:
            raise PlanError(f'第 {i} 个子任务缺少 task：{item!r}')
        if not assignee:
            raise PlanError(f'第 {i} 个子任务缺少 assignee：{item!r}')

        plan.append({'id': item.get('id', i), 'assignee': assignee, 'task': task})

    if not plan:
        raise PlanError('planner 拆出了空的任务列表（没有可并行执行的东西）')

    return plan


# ============================================================================
#  ④ 并行编排器
# ============================================================================

class FanOutOrchestrator(MultiAgentHub):
    """并行编排：拆分 → 并发执行 → 汇总。

    继承 MultiAgentHub 只为了拿"成员名册 + 校验"（这是所有编排策略都需要的
    群级能力）。**不用 _render_view** —— 并行时成员之间上下文隔离，没有
    "别人的话"需要翻译。
    """

    #: planner 的指令模板（放在类属性里，子类可以整体替换，不用改逻辑）
    PLAN_PROMPT = (
        '请把上面的任务拆成若干【可以并行执行】的子任务。要求：\n'
        '1. 子任务之间必须互相独立：任何子任务的输入里都不包含另一个子任务的产出；\n'
        '2. 每个子任务必须指定一个执行者，执行者只能从这些人里选：{names}；\n'
        '3. 只输出 JSON 数组，不要解释、不要 markdown 围栏。格式：\n'
        '[{{"id": 1, "assignee": "执行者名字", "task": "要做什么"}}]'
    )

    def __init__(
        self,
        agents: Mapping[str, Agent],
        planner: Agent,
        synthesizer: Optional[Agent] = None,
        *,
        max_workers: int = 4,
        executor: Optional[TaskExecutor] = None,
        max_tasks: int = 16,
        on_error: str = 'collect',
        serialize_per_agent: bool = True,
        synthesizer_name: str = 'synthesizer',
    ):
        super().__init__(agents)        # 基类：非空校验 + 名字校验 + 复制一份

        if planner is None:
            raise ValueError('必须提供 planner（不然谁来拆任务？）')
        if on_error not in ('collect', 'raise'):
            raise ValueError(f"on_error 只能是 'collect' 或 'raise'，收到 {on_error!r}")
        if max_tasks < 1:
            raise ValueError(f'max_tasks 必须 >= 1，收到 {max_tasks}')

        self.planner = planner
        self.synthesizer = synthesizer
        self.synthesizer_name = synthesizer_name
        self.max_tasks = max_tasks
        self.on_error = on_error
        self.serialize_per_agent = serialize_per_agent

        # ★ 执行器：外部给了就用外部的（max_workers 交给它自己管）
        self._executor: TaskExecutor = executor or ThreadExecutor(max_workers)

        # ★ 每个成员一把锁：同一个成员不并发，不同成员照样并行
        self._locks: Dict[str, threading.Lock] = {n: threading.Lock() for n in self._agents}

        #: 跑完后的体检报告（run 之后可读；调试 / 评测 / tracer 用）
        self.last_report: Optional[ParallelReport] = None

    # ------------------------------------------------------------------
    #  对外入口
    # ------------------------------------------------------------------
    def run(self, messages: Sequence[Message]) -> Iterator[Message]:
        """跑一轮并行编排。惰性：不迭代就什么都不会发生。"""
        yield from self._run(messages)

    # ------------------------------------------------------------------
    #  ① 拆分
    # ------------------------------------------------------------------
    def _plan(self, messages: Sequence[Message]) -> List[SubTask]:
        """问 planner："这活怎么拆？" 并把它的回答解析成 SubTask 列表。"""
        prompt = self.PLAN_PROMPT.format(names=self.agent_names)
        # ★ list(messages)：复制一份，绝不改调用方传进来的列表
        produced = list(self.planner.run(list(messages) + [Message.user(prompt)]))

        if not produced:
            raise PlanError('planner 一条消息都没产出')
        raw = produced[-1].content or ''

        plan = parse_plan(raw)

        if len(plan) > self.max_tasks:
            raise PlanError(
                f'planner 拆出了 {len(plan)} 个子任务，超过上限 max_tasks={self.max_tasks}。'
                f'（太多并发会被 API 限流，也会让成本失控）'
            )

        subtasks = []
        for item in plan:
            assignee = item['assignee']
            # ★ 名字写错 → 当场报错并列出合法名字。不猜、不降级。
            if assignee not in self._agents:
                raise PlanError(
                    f'planner 指定的执行者 {assignee!r} 不在成员名册里。'
                    f'可用成员：{self.agent_names}'
                )
            subtasks.append(SubTask(id=item['id'], assignee=assignee, task=item['task']))

        return subtasks

    # ------------------------------------------------------------------
    #  ② 单个 worker：注意"输入是独立构造的"
    # ------------------------------------------------------------------
    def _run_one(self, sub: SubTask) -> TaskResult:
        """跑一个子任务。**永远不抛业务异常** —— 失败也返回 TaskResult(ok=False)。

        ★ 为什么在这里兜异常，而不是让异常穿过线程池？
          线程池里抛异常会让整个 map() 炸掉，一个子任务失败就毁掉全部结果。
          兜住它 → 其它子任务照常完成，失败的单独记进报告（部分失败策略）。
        """
        t0 = time.time()
        agent = self._agents[sub.assignee]

        # ★★ 独立构造 messages：这是并行的前提。
        #    如果多个 worker 共享同一个 list 并各自 append，就是数据竞争 ——
        #    结果会串味，而且错得毫无规律。
        msgs: List[Message] = [Message.user(sub.task)]

        try:
            ctx = self._locks[sub.assignee] if self.serialize_per_agent else nullcontext()
            with ctx:
                produced = list(agent.run(msgs))

            if not produced:
                raise RuntimeError(f'成员 {sub.assignee} 一条消息都没产出')

            # 最后一个消息就是它的答案（前面可能还有"我想调工具"之类）
            output = produced[-1].content or ''
            return TaskResult(
                id=sub.id, assignee=sub.assignee, task=sub.task,
                ok=True, output=output,
                elapsed=round(time.time() - t0, 4),
            )

        except ControlFlowSignal:
            # ★ 控制流信号（预算超限 / 审批被拒）不属于"这个子任务失败"，
            #   而是"整轮必须停下"。所以放行，不吞。这跟事件总线的处置一致。
            raise

        except Exception as exc:                                   # noqa: BLE001
            return TaskResult(
                id=sub.id, assignee=sub.assignee, task=sub.task,
                ok=False, error=f'{type(exc).__name__}: {exc}',
                elapsed=round(time.time() - t0, 4),
            )

    # ------------------------------------------------------------------
    #  ③ 汇总
    # ------------------------------------------------------------------
    def _synthesize(self, messages: Sequence[Message],
                    results: List[TaskResult]) -> Iterator[Message]:
        """把 N 个子结果收成一个答案。

        synthesizer 为 None 时退化成"直接拼接"——适合"本来就要一份清单"的场景。
        """
        ok = [r for r in results if r.ok]

        # ---- 退化路径：不汇总，一条条吐出去 ----
        if self.synthesizer is None:
            if not ok:
                yield Message.assistant('[并行编排] 所有子任务都失败了，没有可汇总的结果。')
                return
            for r in ok:
                yield Message.assistant(f'[{r.assignee}] {r.output}', agent_name=r.assignee)
            return

        # ---- 正常路径：把子结果打包成一条 user，交给 synthesizer ----
        blocks: List[str] = []
        for r in ok:
            blocks.append(f'### 子任务 {r.id}（{r.assignee}）\n任务：{r.task}\n结果：{r.output}')
        for r in results:
            if not r.ok:                       # ★ 失败也要告诉汇总者，别假装成功
                blocks.append(f'### 子任务 {r.id}（{r.assignee}）失败\n原因：{r.error}')

        body = '\n\n'.join(blocks) if blocks else '（没有任何子任务结果）'
        ask = (
            '下面是多个并行子任务的结果，请把它们汇总成一份完整、连贯的最终答案。'
            '不要罗列"子任务1/子任务2"，要像一个人写的一样。\n\n' + body
        )

        # 原样保留调用方的 system 消息（人设 / 约束都在里面）
        base = [m for m in messages if m.role == 'system']
        produced = list(self.synthesizer.run(base + [Message.user(ask)]))

        for m in produced:
            m.agent_name = self.synthesizer_name      # 盖上"谁说的"
            yield m

    # ------------------------------------------------------------------
    #  主流程
    # ------------------------------------------------------------------
    def _run(self, messages: Sequence[Message]) -> Iterator[Message]:
        t0 = time.time()

        subtasks = self._plan(messages)                        # ① 拆分（**必须**在这一步就失败得很清楚）
        results = self._executor.map(self._run_one, subtasks)  # ② 并发（顺序与 subtasks 一致）
        wall = round(time.time() - t0, 4)
        self.last_report = ParallelReport(results=results, wall_elapsed=wall)

        # ③ 失败策略
        if self.on_error == 'raise' and self.last_report.failed_count:
            bad = self.last_report.failed_results
            detail = '; '.join(f'#{r.id}({r.assignee}): {r.error}' for r in bad)
            raise RuntimeError(f'{len(bad)} 个子任务失败：{detail}')

        # ④ 汇总
        yield from self._synthesize(messages, results)
