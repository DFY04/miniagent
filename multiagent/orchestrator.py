"""P14 · Orchestrator —— 多智能体编排（等价于 qwen-agent 的 GroupChat auto 模式）。

════════════════════════════════════════════════════════════════════
【一句话定义】
    Orchestrator 是"指挥"：它自己不干活，只决定"该谁上"。

        Agent            —— 干活的人   （run(messages) 里想 → 做 → 观察）
        MultiAgentHub    —— 存人 + 身份转换（基类，见 hub.py）
        Orchestrator     —— 派活的人   （本文件：顺序策略）

════════════════════════════════════════════════════════════════════
【★ 整个框架只有一件事：动态多轮编排】

    每轮做四步，循环 max_round 次：

        ① 问裁判："下一个该谁？"            -> _pick_next
        ② 把共享历史渲染成那个人的视角       -> _render_view（基类提供）
        ③ 让他上，拿产出                    -> _ask
        ④ 给产出盖上 agent_name，写回账本    -> _run 里的两行

    直到：裁判说 '[STOP]'，或者跑满 max_round 轮。

    ─────────────── 为什么只有这一个模式 ───────────────

    原本想过三个模式，砍到只剩这一个，因为另外两个都是它的**退化解**：

        sequential（固定顺序）  = 换个"选人方式"就是它
        router（一次性分诊）    = max_round=1 就是它

        ┌────────────┬──────────────┬────────────┐
        │            │ 顺序谁定     │ 上下文累积 │
        ├────────────┼──────────────┼────────────┤
        │ sequential │ 代码         │ ✅         │  ← 手写 3 行 for 循环更简单
        │ router     │ LLM          │ ❌         │  ← max_round=1
        │ handoff    │ LLM          │ ✅         │  ← 本类
        └────────────┴──────────────┴────────────┘

    ★ 如果顺序**必须**固定（"批判"跑在"分析"前面逻辑上就无意义），
      那就不该用这个类 —— 写 3 行 for 循环，顺序由 Python 语法硬保证，
      比任何 prompt 都可靠。

      这个类解决的是"**顺序需要动态决定**"的问题（谁合适谁上）。
      别为了"统一"把简单的事装进框架。

════════════════════════════════════════════════════════════════════
【★ 身份从哪来：agent_name 字段 ★】

    Message 的两个字段分工**互不重叠**：

        name        —— 只表示"哪个工具"（只对 role='function' 有意义）
        agent_name  —— 只表示"谁说的"（四种 role 全都有效）★ 编排层用它

    为什么要拆成两个？因为上游（OpenAI / qwen-agent）只有一个 name，
    它的含义随 role 变 —— `m.name == me` 这句判断看着没毛病，
    实际同时在对"工具名"和"说话人"做比较，迟早写错且不报错。

    ★ 盖章的地方只有一处：

        for m in produced:
            m.agent_name = name      # ← 唯一一处给产出盖章
            yield m
        shared.extend(produced)      # 同一批对象，账本里那批也带着章了

    ★ 为什么给**所有**产出盖章，而不是只盖 assistant？
        一个 Agent 调工具时会产出三种消息：

            assistant(function_call=...)   ← 我想调工具      agent_name = '分析师'
            function(name='sql_query', ...) ← 工具结果       agent_name = '分析师'
            assistant('查到了 3 条记录')     ← 最终答案       agent_name = '分析师'

        如果只盖 assistant，中间那条 function 消息的 agent_name 是 None，
        就分不清"这是我自己调工具的过程"还是"别人调工具的过程"。
        全盖上之后，_render_view 里一眼就能分开（见 hub.py 的 ②③ 分支）。

    ★ agent_name **不发给 LLM API**：
        因为 _render_view 已经把"谁说的"编进了文本（`说话人: 内容`），
        模型不需要额外的字段。所以 miniagent/llm.py 的适配器不用改，
        它只透传 name（对 assistant 消息永远是 None）—— 行为跟以前一模一样。
        这是这个改动风险几乎为零的原因。

════════════════════════════════════════════════════════════════════
【契约】

    Orchestrator(agents, router_agent, max_round=3)
        agents        —— 名字 -> Agent（交给基类校验与保存）
        router_agent  —— 裁判。必须提供
        max_round     —— 最多跑几轮，必须 >= 1，否则 ValueError

    run(messages) -> Iterator[Message]
        —— 透传内部生成器（流式），不要先 list() 再 return
        —— 每条产出都盖上 agent_name
        —— 只 yield "新产出"，不吐调用方传进来的输入
        —— 裁判的提问和回答都是内部过程，**不 yield 出去**
        —— 跑满 max_round 后**不许再多问一次裁判**

    _pick_next(shared) -> str | None
        1. 构造提问：候选人是 [名字列表]，只输出一个名字，该结束就输出 [STOP]
        2. 喂给 router_agent 的 messages =
               _render_view(self.NOBODY, shared) + [Message.user(提问)]
        3. 取 router 的最后一条回答，strip()
        4. 命中成员名   -> 返回它
           等于 STOP     -> 返回 None
           其他（乱说）  -> 兜底返回**第一个**成员的名字
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Sequence, Tuple

from miniagent.core.agent import Agent
from miniagent.core.message import Message
from miniagent.multiagent.hub import MultiAgentHub


class Orchestrator(MultiAgentHub):
    """多智能体编排：每轮让裁判决定下一个是谁，上下文累积。"""

    def __init__(
        self,
        agents: Dict[str, Agent],
        router_agent: Agent,
        max_round: int = 3,
    ):
        super().__init__(agents)          # 基类负责：非空校验 + 复制 + 存住

        if max_round < 1:
            raise ValueError(f'max_round 必须 >= 1，收到 {max_round}')
        if router_agent is None:
            raise ValueError('必须提供 router_agent（不然谁来决定下一个是谁？）')

        self.router_agent = router_agent
        self.max_round = max_round

    # ================= 对外入口 =================

    def run(self, messages: Sequence[Message]) -> Iterator[Message]:
        """把内部生成器透传出去。

        用 `yield from` 而不是 `return self._run(...)` 的等价写法：
        凡是函数体里出现 yield，这个函数就变成**生成器函数** ——
        调用它不会执行任何代码，要等第一次 next() 才开始跑。
        这就是"惰性"（测试里有 test_run_is_lazy_until_iterated 盯着）。
        """
        yield from self._run(messages)

    # ================= 三个零件 =================

    @staticmethod
    def _ask(agent: Agent, messages: List[Message]) -> Tuple[List[Message], Message]:
        """让一个 Agent 跑完，返回 (它产出的全部消息, 最后一条消息)。

        ★ 坑：agent.run(messages) 返回的是**生成器**，不是结果。
          直接把它当列表或字符串用，什么都拿不到 —— 必须"耗光"它。
          `list(...)` 就是最直接的耗光方式。
        """
        produced = list(agent.run(messages))
        if not produced:
            raise RuntimeError(f'Agent {agent!r} 一条消息都没产出')
        return produced, produced[-1]

    def _pick_next(self, shared: Sequence[Message]) -> str | None:
        """问裁判："下一个该谁？" 返回成员名；返回 None 表示 '[STOP]'。"""
        names = self.agent_names
        prompt = (f'候选人是 {names}。\n'
                  f'请只输出下一个该发言的人的名字，不要输出其他任何内容；\n'
                  f'如果这个话题应该结束了、没人需要再说话，就输出 {self.STOP}。')

        # ★ 用哨兵名渲染 → 裁判拿到的是"上帝视角"：所有人的话合成一条 user
        view = self._render_view(self.NOBODY, shared)
        _, last = self._ask(self.router_agent, view + [Message.user(prompt)])

        answer = (last.content or '').strip()      # 模型可能带空白/换行
        if answer in self._agents:
            return answer
        if answer == self.STOP:
            return None
        return names[0]                            # 兜底：模型乱说就交给第一个人

    # ================= 主循环 =================

    def _run(self, messages: Sequence[Message]) -> Iterator[Message]:
        """每轮：判定谁上 → 渲染视角 → 跑 → 盖 agent_name → yield → 记进账本。"""
        shared = list(messages)                    # ★ 复制，别动调用方的 list

        for _ in range(self.max_round):            # ★ 唯一的硬刹车
            name = self._pick_next(shared)
            if name is None:                       # 裁判说 '[STOP]'
                return

            view = self._render_view(name, shared)         # 身份转换
            produced, _final = self._ask(self._agents[name], view)

            for m in produced:
                m.agent_name = name                # ★ 唯一一处盖章
                yield m

            shared.extend(produced)                # 交棒：产出进共享历史
