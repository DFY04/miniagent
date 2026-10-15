"""test_p06_budget.py —— P06 验收测试（全离线：不联网、不花一分钱）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p06_budget.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p06_budget

验收标准（DAILY_PLAN 里的两条 + 必要的回归）：
    [x] 造一个超预算的 run，被优雅中止并给出 reason='budget'
    [x] 未超限时不误报
    [+] 记账正确：步数只数 STEP_END、成本用【增量】而不是累计（防"重复累加"复发）
    [+] BudgetExceeded 必须能穿过事件总线（ControlFlowSignal）
    [+] 忘了 attach → 记 0 账（把这个坑固化下来）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import ControlFlowSignal, EventBus, EventType  # noqa: E402
from miniagent.core.mem_agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.core.tool import BaseTool  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402
from miniagent.runtime.budget import Budget, BudgetExceeded  # noqa: E402


# ==================== 假零件 ====================

class LoopForeverLLM:
    """永远要求调工具 —— 模拟"模型死循环、自己不收手"。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return Message.assistant('', function_call=FunctionCall(name='echo', arguments='{}'))


class AnswerOnceLLM:
    """一次就给最终答案 —— 模拟正常结束。"""

    def chat(self, messages, tools=None):
        return Message.assistant('好了')


class EchoTool(BaseTool):
    name = 'echo'
    description = '回显'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, args):
        return 'ok'


def _agent(llm, budget=None, bus=None, max_steps=10):
    return Agent(
        llm=llm,
        system_message='测试助手',
        tools=[EchoTool()],
        max_steps=max_steps,
        mem=ShortTermMemory(max_tokens=100000),
        budget=budget,
        bus=bus,
    )


def _budget_attached(**kw):
    bus = EventBus()
    b = Budget(**kw)
    b.attach(bus)
    return b, bus


# ==================== 记账 ====================

def test_run_start_resets_counters():
    """新 run 开始要清零（Budget 靠 RUN_START 事件知道"新一轮来了"）。"""
    b, bus = _budget_attached()
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.STEP_END, step=0)
    bus.emit(EventType.STEP_END, step=1)
    assert b.used_steps == 2

    bus.emit(EventType.RUN_START)          # 第二轮
    assert b.used_steps == 0, 'RUN_START 必须清零'


def test_only_step_end_counts_steps():
    """★ 只数 STEP_END，别的事件不能算"一步"。"""
    b, bus = _budget_attached()
    bus.emit(EventType.RUN_START)
    for ev in (EventType.RAG_START, EventType.RAG_END, EventType.LLM_START,
               EventType.LLM_END, EventType.TOOL_START, EventType.TOOL_END):
        bus.emit(ev)
    assert b.used_steps == 0, f'这些都不是"步"，但数成了 {b.used_steps}'

    bus.emit(EventType.STEP_END, step=0)
    assert b.used_steps == 1


def test_llm_end_accumulates_tokens():
    b, bus = _budget_attached()
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_END, reply=Message.assistant('你好'))
    first = b.used_tokens
    assert first > 0
    bus.emit(EventType.LLM_END, reply=Message.assistant('你好'))
    assert b.used_tokens == first * 2, '两次相同长度输出，token 应该正好翻倍'


def test_cost_uses_increment_not_total():
    """★★ 回归测试：成本必须用【本次增量】算。

    写成 `self.used_cost += self.used_tokens / 1000 * price`（累计值）的话，
    成本会平方级偏高（3 次偏 2 倍、100 次偏约 50 倍），预算会提前误报。
    """
    b, bus = _budget_attached(price_per_1k=0.0014)
    bus.emit(EventType.RUN_START)
    for _ in range(5):
        bus.emit(EventType.LLM_END, reply=Message.assistant('你好'))

    expected = b.used_tokens / 1000 * 0.0014
    assert abs(b.used_cost - expected) < 1e-12, \
        f'成本算错了：{b.used_cost} != {expected}（是不是又用了累计 token？）'


def test_forgot_attach_records_nothing():
    """★ 忘了 budget.attach(bus) → 一步账都不记（静默失效，最坑的一种）。

    这正是 P06 集成时最容易踩的坑：Agent 传了 budget，却没把 budget 挂到总线上。
    """
    bus = EventBus()
    b = Budget(max_steps=1)               # 故意不 attach
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.STEP_END, step=0)
    assert b.used_steps == 0, '没 attach 却记上账了？'
    b.check()                             # 因此永远不会抛


# ==================== check ====================

def test_check_passes_when_under_budget():
    """★ 验收②：未超限不能误报。"""
    b, bus = _budget_attached(max_steps=5, max_tokens=100, max_cost_usd=1.0)
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.STEP_END, step=0)
    bus.emit(EventType.LLM_END, reply=Message.assistant('短'))
    b.check()                             # 不该抛


def test_check_raises_on_steps():
    b, bus = _budget_attached(max_steps=1)
    bus.emit(EventType.RUN_START)
    for i in range(3):
        bus.emit(EventType.STEP_END, step=i)
    try:
        b.check()
        assert False, '步数超了却没抛'
    except BudgetExceeded as e:
        assert str(e), '异常消息里应该带上数据（用了多少 / 上限多少）'


def test_check_raises_on_tokens():
    b, bus = _budget_attached(max_tokens=1)
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_END, reply=Message.assistant('这句话肯定超过一个 token'))
    try:
        b.check()
        assert False, 'token 超了却没抛'
    except BudgetExceeded:
        pass


def test_check_raises_on_cost():
    b, bus = _budget_attached(max_cost_usd=1e-9)
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_END, reply=Message.assistant('你好'))
    try:
        b.check()
        assert False, '成本超了却没抛'
    except BudgetExceeded:
        pass


def test_none_limits_are_ignored():
    """没配的上限不该检查（max_tokens=None 表示"不限"）。"""
    b, bus = _budget_attached()          # 三个上限都不给
    bus.emit(EventType.RUN_START)
    for i in range(100):
        bus.emit(EventType.STEP_END, step=i)
        bus.emit(EventType.LLM_END, reply=Message.assistant('很长很长的一句话' * 10))
    b.check()                            # 不该抛


def test_budget_exceeded_is_control_flow_signal():
    """★ BudgetExceeded 必须是 ControlFlowSignal，否则会被事件总线的异常隔离吞掉。"""
    assert issubclass(BudgetExceeded, ControlFlowSignal)


def test_exception_can_pass_through_bus():
    """实测：预算异常从事件回调里抛出，能穿过总线冒到调用方。"""
    bus = EventBus()

    def handler(**kw):
        raise BudgetExceeded('测试用')

    bus.subscribe(EventType.RUN_START, handler)
    try:
        bus.emit(EventType.RUN_START)
        assert False, '异常被总线吞掉了（BudgetExceeded 没继承 ControlFlowSignal？）'
    except BudgetExceeded:
        pass


# ==================== 接进 Agent ====================

def _run_and_collect(agent, question='一直干下去'):
    """跑一轮，返回 (yield 出来的消息列表)。"""
    return list(agent.run([Message.user(question)]))


def test_acceptance_budget_aborts_gracefully():
    """★ 验收①：超预算的 run 被优雅中止，reason='budget'。"""
    reasons = []
    bus = EventBus()
    bus.subscribe(EventType.RUN_END, lambda **kw: reasons.append(kw.get('reason')))

    budget = Budget(max_steps=2)
    budget.attach(bus)                       # ★ 必须挂上总线
    agent = _agent(LoopForeverLLM(), budget=budget, bus=bus, max_steps=10)

    msgs = _run_and_collect(agent)

    assert reasons[-1] == 'budget', f'RUN_END 应该报 budget，实际 {reasons}'
    assert msgs[-1].content.startswith('[已超出预算'), msgs[-1].content
    assert '超限' in msgs[-1].content, '中止消息里要说明超了什么'


def test_acceptance_no_false_positive():
    """★ 验收②：预算充足时，正常跑完，reason='done'（不能误报）。"""
    reasons = []
    bus = EventBus()
    bus.subscribe(EventType.RUN_END, lambda **kw: reasons.append(kw.get('reason')))

    budget = Budget(max_steps=10, max_tokens=100000, max_cost_usd=100.0)
    budget.attach(bus)
    agent = _agent(AnswerOnceLLM(), budget=budget, bus=bus)

    _run_and_collect(agent, '你好')
    assert reasons[-1] == 'done', f'不该误报预算，实际 {reasons}'


def test_agent_without_budget_still_works():
    """不传 budget → 行为和以前一样（不能因为加了预算把老路径搞坏）。"""
    reasons = []
    bus = EventBus()
    bus.subscribe(EventType.RUN_END, lambda **kw: reasons.append(kw.get('reason')))

    agent = _agent(AnswerOnceLLM(), budget=None, bus=bus)
    _run_and_collect(agent, '你好')
    assert reasons[-1] == 'done'


def test_budget_uses_exactly_n_steps():
    """★★ 边界：max_steps=N 应该允许【N】步，不是 N+1 步。

    check() 写在每一步开头、used_steps 在 STEP_END 才 +1，
    所以比较符用 `>` 会多放行一步（2 变成 3）；要用 `>=`。

    ★ 这一条现在会红 —— 你要自己决定：改代码（> → >=）还是改这条期望。
    """
    bus = EventBus()
    budget = Budget(max_steps=2)
    budget.attach(bus)
    agent = _agent(LoopForeverLLM(), budget=budget, bus=bus, max_steps=10)

    _run_and_collect(agent)
    assert budget.used_steps == 2, \
        f'预算给 2 步，实际跑了 {budget.used_steps} 步（典型 off-by-one）'


# ==================== 脚本模式入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  ✅ {name}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {name}  ->  {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
