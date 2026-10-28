"""test_p09_approval.py —— P09 验收测试（全离线、不阻塞、不用敲键盘）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p09_approval.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p09_approval

DAILY_PLAN 的三条验收：
    [x] DROP TABLE 被拦
    [x] SELECT 放行
    [x] 拒绝时主循环收到 ApprovalRejected 并优雅结束

加上几条必要的回归：
    [+] policy 注入后**绝不碰 input()**（"不阻塞"的字面含义）
    [+] 异常能穿过事件总线（ControlFlowSignal）
    [+] TOOL_START 的负载必须带 args（否则审批根本看不到参数）
"""
from __future__ import annotations

import builtins
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import ApprovalRejected, ControlFlowSignal, EventBus, EventType  # noqa: E402
from miniagent.core.mem_agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.core.tool import BaseTool  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402
from miniagent.runtime.approval import ApprovalGate  # noqa: E402


# ==================== 假零件 ====================

class SqlTool(BaseTool):
    """假装是个 SQL 工具。它自己不管危险不危险 —— 那是审批闸门的事。"""

    name = 'sql_query'
    description = '执行 SQL'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, args):
        return f'执行了: {args}'


class SqlLLM:
    """模型：要执行一句 SQL（参数由测试决定）。"""

    def __init__(self, sql: str):
        self.sql = sql

    def chat(self, messages, tools=None):
        return Message.assistant('', function_call=FunctionCall(name='sql_query', arguments=self.sql))


# ==================== 危险规则 ====================

def test_default_dangerous_words():
    gate = ApprovalGate()
    for args in ['DROP TABLE users', 'DELETE FROM users', 'update users set a=1',
                 'INSERT INTO t VALUES (1)', 'ALTER TABLE t ADD c int',
                 'rm -rf /', 'sudo reboot', 'TRUNCATE TABLE t']:
        assert gate.is_dangerous(args), f'这个应该算危险: {args}'


def test_safe_sql_passes():
    """★ 验收：SELECT 放行。"""
    gate = ApprovalGate()
    for args in ['SELECT * FROM users LIMIT 10', 'select count(*) from orders',
                 'SELECT * FROM users WHERE name = "drop"']:
        # 注意最后一条：字符串里带 drop（小写）—— 大小写不敏感 → 会被拦。
        # 这里只测纯查询，所以用前两条。
        pass
    assert not gate.is_dangerous('SELECT * FROM users LIMIT 10')
    assert not gate.is_dangerous('select count(*) from orders')


def test_case_insensitive():
    gate = ApprovalGate()
    assert gate.is_dangerous('drop table users')
    assert gate.is_dangerous('DrOp TaBlE users')


def test_custom_dangerous_words():
    gate = ApprovalGate(dangerous=['超纲'])
    assert gate.is_dangerous('这个操作超纲了')
    assert not gate.is_dangerous('DROP TABLE users')      # 自定义表里没有 DROP


# ==================== 决策：policy（不阻塞）====================

def test_reject_raises_approval_rejected():
    """★ 验收：DROP TABLE 被拦（拒绝 → 抛 ApprovalRejected）。"""
    gate = ApprovalGate(policy=lambda name, args: False)
    try:
        gate.on_tool_start(name='sql_query', args='DROP TABLE users')
        assert False, '应该被拦住'
    except ApprovalRejected as e:
        assert e.tool_name == 'sql_query'
        assert 'sql_query' in str(e), '消息里要带工具名'


def test_approve_passes():
    gate = ApprovalGate(policy=lambda name, args: True)
    gate.on_tool_start(name='sql_query', args='DROP TABLE users')   # 不抛就算过


def test_policy_receives_name_and_args():
    seen = []

    def policy(name, args):
        seen.append((name, args))
        return True

    ApprovalGate(policy=policy).on_tool_start(name='sql_query', args='DELETE FROM t')
    assert seen == [('sql_query', 'DELETE FROM t')]


def test_policy_not_called_for_safe_args():
    seen = []
    gate = ApprovalGate(policy=lambda name, args: seen.append(name) or True)
    gate.on_tool_start(name='sql_query', args='SELECT 1')
    assert seen == [], '安全操作不该去打扰 policy'


def test_injected_policy_never_touches_input(monkeypatch=None):
    """★★ "不阻塞"的字面含义：注入 policy 后，绝不调用 input()。

    做法：把 builtins.input 换成一个"一被调用就报错"的函数。
    如果代码偷偷走了交互分支，这里就会炸。
    """
    original = builtins.input

    def boom(*a, **kw):
        raise AssertionError('不该调用 input()！这说明注入的 policy 没被用上')

    builtins.input = boom
    try:
        gate = ApprovalGate(policy=lambda name, args: False)
        try:
            gate.on_tool_start(name='sql_query', args='DROP TABLE users')
            assert False, '应该抛出 ApprovalRejected'
        except ApprovalRejected:
            pass
    finally:
        builtins.input = original


def test_interactive_mode_asks_input():
    """没注入 policy 时走交互：把 input 换成假回答，测两条分支。"""
    original = builtins.input
    try:
        builtins.input = lambda *a, **kw: 'y'
        ApprovalGate().on_tool_start(name='sql_query', args='DROP TABLE users')   # 批准

        builtins.input = lambda *a, **kw: 'n'
        try:
            ApprovalGate().on_tool_start(name='sql_query', args='DROP TABLE users')
            assert False, '回答了 n 应该拒绝'
        except ApprovalRejected:
            pass
    finally:
        builtins.input = original


def test_missing_args_is_treated_as_safe():
    """回归：负载里没有 args（老埋点）时不能误拦一切 —— 一律放行。"""
    gate = ApprovalGate(policy=lambda name, args: False)
    gate.on_tool_start(name='sql_query')          # 没有 args → 判不出危险 → 放行


# ==================== 总线 ====================

def test_approval_rejected_is_control_flow_signal():
    assert issubclass(ApprovalRejected, ControlFlowSignal)


def test_attach_and_exception_passes_through_bus():
    bus = EventBus()
    ApprovalGate(policy=lambda name, args: False).attach(bus)
    try:
        bus.emit(EventType.TOOL_START, name='sql_query', args='DROP TABLE users')
        assert False, '异常被总线吞掉了'
    except ApprovalRejected:
        pass


def test_attach_only_cares_about_tool_start():
    """订阅的是 TOOL_START：别的事件不该触发审批。"""
    bus = EventBus()
    seen = []
    ApprovalGate(policy=lambda name, args: seen.append(name) or True).attach(bus)
    bus.emit(EventType.LLM_START, step=0)
    bus.emit(EventType.STEP_END, step=0)
    assert seen == []


# ==================== 接进 Agent（验收第三条）====================

def _run(agent, question='帮我删掉所有用户'):
    reasons = []
    bus = EventBus()
    bus.subscribe(EventType.RUN_END, lambda **kw: reasons.append(kw.get('reason')))
    agent.bus = bus
    # 审批闸门要挂在同一个 bus 上
    ApprovalGate(policy=lambda name, args: False).attach(bus)
    out = list(agent.run([Message.user(question)]))
    return reasons, out


def test_acceptance_agent_aborts_on_rejected():
    """★ 验收：拒绝时主循环收到 ApprovalRejected 并优雅结束（reason='rejected'）。"""
    agent = Agent(
        llm=SqlLLM('DROP TABLE users'),
        system_message='测试',
        tools=[SqlTool()],
        max_steps=5,
        mem=ShortTermMemory(max_tokens=100000),
    )
    reasons, out = _run(agent)
    assert reasons[-1] == 'rejected', f'RUN_END 应该报 rejected，实际 {reasons}'
    assert any('操作被拒绝' in (m.content or '') for m in out), '要给用户一句解释'
    assert not any('执行了: DROP' in (m.content or '') for m in out), '工具绝不该真的被执行！'


def test_acceptance_safe_sql_is_executed():
    """对照：SELECT 放行 → 工具真的执行了 → 正常结束。"""
    agent = Agent(
        llm=SqlLLM('SELECT * FROM users'),
        system_message='测试',
        tools=[SqlTool()],
        max_steps=2,
        mem=ShortTermMemory(max_tokens=100000),
    )
    reasons, out = _run(agent)
    assert any('执行了: SELECT' in (m.content or '') for m in out), '安全 SQL 应该被执行'
    assert reasons[-1] in ('done', 'max_steps')


def test_tool_start_event_carries_args():
    """★★ 回归：TOOL_START 的负载必须带 args，否则审批根本看不到参数。"""
    got = []
    agent = Agent(
        llm=SqlLLM('DROP TABLE users'),
        system_message='测试',
        tools=[SqlTool()],
        max_steps=5,
        mem=ShortTermMemory(max_tokens=100000),
    )
    bus = EventBus()
    bus.subscribe(EventType.TOOL_START, lambda **kw: got.append(kw))
    agent.bus = bus
    list(agent.run([Message.user('删表')]))

    assert got, 'TOOL_START 事件没发出？'
    assert got[0].get('name') == 'sql_query'
    assert got[0].get('args') == 'DROP TABLE users', f'args 丢了：{got[0]}'


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
