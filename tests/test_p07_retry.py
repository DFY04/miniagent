"""test_p07_retry.py —— P07 验收测试（全离线、不联网、不等真实睡眠）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p07_retry.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p07_retry

分三组：
    A. with_retry    —— 重试与退避序列
    B. LoopDetector  —— 死循环检测
    C. 接进 Agent    —— ★ 还没接线，现在会红（这是你的作业）

关键测试技巧：**把 sleep 打成假的**（`sleep=lambda s: sleeps.append(s)`），
既能断言"等了多久"，又不用真等 1+2+4 秒。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import ControlFlowSignal, EventBus, EventType  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.core.tool import BaseTool  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402
from miniagent.runtime.retry import LoopDetected, LoopDetector, _digest, with_retry  # noqa: E402


# ==================== A. with_retry ====================

def test_retry_returns_on_first_success():
    sleeps = []
    out = with_retry(lambda: 'ok', sleep=sleeps.append)
    assert out == 'ok'
    assert sleeps == [], '一次就成功，不该睡'


def test_retry_succeeds_after_two_failures():
    calls = {'n': 0}
    sleeps = []

    def flaky():
        calls['n'] += 1
        if calls['n'] < 3:
            raise TimeoutError(f'第{calls["n"]}次失败')
        return '终于成功'

    out = with_retry(flaky, sleep=sleeps.append)
    assert out == '终于成功'
    assert calls['n'] == 3, '应该调用 3 次'
    assert sleeps == [1.0, 2.0], f'退避序列应该是 1s、2s，实际 {sleeps}'


def test_retry_raises_last_exception_when_exhausted():
    def always_fail():
        raise TimeoutError('连不上')

    try:
        with_retry(always_fail, max_retries=2, sleep=lambda s: None)
        assert False, '用尽次数后应该抛异常'
    except TimeoutError as e:
        assert '连不上' in str(e), '要原样抛出（上层才能看到真因）'


def test_retry_call_count_is_max_retries_plus_one():
    calls = {'n': 0}

    def always_fail():
        calls['n'] += 1
        raise RuntimeError('boom')

    try:
        with_retry(always_fail, max_retries=3, sleep=lambda s: None)
    except RuntimeError:
        pass
    assert calls['n'] == 4, f'首次 + 3 次重试 = 4 次调用，实际 {calls["n"]}'


def test_retry_respects_base_delay():
    sleeps = []
    try:
        with_retry(lambda: (_ for _ in ()).throw(RuntimeError('x')),
                   max_retries=3, base_delay=0.5, sleep=sleeps.append)
    except RuntimeError:
        pass
    assert sleeps == [0.5, 1.0, 2.0], f'base_delay=0.5 → 0.5/1/2，实际 {sleeps}'


def test_retry_max_retries_zero_means_try_once():
    calls = {'n': 0}
    sleeps = []

    def boom():
        calls['n'] += 1
        raise RuntimeError('x')

    try:
        with_retry(boom, max_retries=0, sleep=sleeps.append)
    except RuntimeError:
        pass
    assert calls['n'] == 1
    assert sleeps == [], 'max_retries=0 时不该睡'


def test_retry_only_retries_whitelisted_exceptions():
    """★ 参数写错这种错误，重试 100 次也没用 —— 要能限定只重试哪类异常。"""
    calls = {'n': 0}
    sleeps = []

    class PermanentError(Exception):
        pass

    def bad_args():
        calls['n'] += 1
        raise PermanentError('参数不合法')

    try:
        with_retry(bad_args, max_retries=5, sleep=sleeps.append, retry_on=TimeoutError)
        assert False, '不该重试非白名单异常'
    except PermanentError:
        pass
    assert calls['n'] == 1, f'应该只调用 1 次，实际 {calls["n"]}'
    assert sleeps == [], '不该睡'


def test_retry_on_retry_callback():
    seen = []
    try:
        with_retry(lambda: (_ for _ in ()).throw(RuntimeError('boom')),
                   max_retries=2, sleep=lambda s: None,
                   on_retry=lambda attempt, wait, err: seen.append((attempt, wait, str(err))))
    except RuntimeError:
        pass
    assert seen == [(1, 1.0, 'boom'), (2, 2.0, 'boom')], f'回调内容不对：{seen}'


# ==================== B. LoopDetector ====================

def test_loop_detected_after_three_identical_calls():
    d = LoopDetector(window=3)
    d.check('now', '{}')          # 第 1 次 OK
    d.check('now', '{}')          # 第 2 次 OK
    try:
        d.check('now', '{}')      # 第 3 次 → 抛
        assert False, '连着 3 次一模一样应该被拦住'
    except LoopDetected as e:
        assert e.tool_name == 'now'
        assert 'now' in str(e), '消息里要带工具名，便于排查'


def test_different_args_not_flagged():
    d = LoopDetector(window=3)
    d.check('add', '{"a": 1}')
    d.check('add', '{"a": 2}')
    d.check('add', '{"a": 3}')    # 参数一直在变 → 不是死循环


def test_different_tool_not_flagged():
    d = LoopDetector(window=3)
    d.check('now', '{}')
    d.check('now', '{}')
    d.check('add', '{}')          # 换了工具 → 放过（模型在换策略）


def test_window_rolls_so_interrupted_repeat_is_ok():
    """A B A A（window=3）不该报 —— 中间被打断过。"""
    d = LoopDetector(window=3)
    for t in ['now', 'add', 'now', 'now']:
        d.check(t, '{}')


def test_window_two_flags_on_second_call():
    d = LoopDetector(window=2)
    d.check('now', '{}')
    try:
        d.check('now', '{}')
        assert False, 'window=2 时第 2 次就该拦'
    except LoopDetected:
        pass


def test_reset_clears_window():
    d = LoopDetector(window=3)
    d.check('now', '{}')
    d.check('now', '{}')
    d.reset()                     # 新的一轮 run
    d.check('now', '{}')          # 重置后重新数
    d.check('now', '{}')


def test_loop_detected_is_control_flow_signal():
    """★ 必须是 ControlFlowSignal，否则会被事件总线的异常隔离吞掉。"""
    assert issubclass(LoopDetected, ControlFlowSignal)


def test_loop_detected_message_survives():
    """★ 回归：异常类里**不能**写 `self.args = ...`（那是内置的消息字段）。

    实测过的坑：`self.args = '{}'` 会被 args 的 setter 转成元组 `('{', '}')`，
    于是 str(e) 变成 "('{', '}')"，消息被挤掉。
    """
    e = LoopDetected('now', '{}', times=3)
    assert 'now' in str(e), f'消息被弄丢了：{str(e)!r}'
    assert len(e.args) == 1, 'args 里只该有一条消息'
    assert e.tool_args == '{}', '工具参数要存在自己的字段里'


def test_exception_passes_through_event_bus():
    """止损异常要能穿过总线到主循环。"""
    bus = EventBus()
    bus.subscribe(EventType.STEP_END, lambda **kw: (_ for _ in ()).throw(LoopDetected('now', '{}', 3)))
    try:
        bus.emit(EventType.STEP_END, step=0)
        assert False, '异常被总线吞掉了'
    except LoopDetected:
        pass


def test_digest_is_stable_and_distinguishes():
    assert _digest('now', '{}') == _digest('now', '{}'), '同输入必须同指纹'
    assert _digest('now', '{}') != _digest('now', '{ }'), '不同输入要能区分'
    assert _digest('ab', 'c') != _digest('a', 'bc'), '拼接要有分隔符，别撞车'


# ==================== C. 接进 Agent（还没接线，现在会红）====================

class LoopForeverLLM:
    """永远要求用同一组参数调同一个工具。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return Message.assistant('', function_call=FunctionCall(name='echo', arguments='{}'))


class FlakyLLM:
    """第一次调用失败，之后正常 —— 用来验证"重试救回偶发失败"。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError('第一次：网络抖了')
        return Message.assistant('好了')


class EchoTool(BaseTool):
    name = 'echo'
    description = '回显'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, args):
        return 'ok'


def _reasons_of(agent, question='一直干下去'):
    reasons = []
    bus = EventBus()
    bus.subscribe(EventType.RUN_END, lambda **kw: reasons.append(kw.get('reason')))
    agent.bus = bus
    list(agent.run([Message.user(question)]))
    return reasons


def test_acceptance_loop_is_stopped():
    """★ 验收①：反复调同一工具同一参数 → 被检出并中止（reason='loop'）。

    需要 Agent 支持 loop_detector 参数（见 mem_agent 的接线作业）。
    """
    from miniagent.core.mem_agent import Agent

    agent = Agent(
        llm=LoopForeverLLM(), system_message='测试', tools=[EchoTool()], max_steps=10,
        mem=ShortTermMemory(max_tokens=100000), loop_detector=LoopDetector(window=3),
    )
    reasons = _reasons_of(agent)
    assert reasons[-1] == 'loop', f'RUN_END 应该报 loop，实际 {reasons}'


def test_acceptance_llm_retry_saves_one_off_failure():
    """★ 验收②：LLM 偶发失败被重试救回，整轮照常结束（reason='done'）。"""
    from miniagent.core.mem_agent import Agent

    llm = FlakyLLM()
    agent = Agent(
        llm=llm, system_message='测试', tools=[EchoTool()], max_steps=3,
        mem=ShortTermMemory(max_tokens=100000), max_retries=2, sleep=lambda s: None,
    )
    reasons = _reasons_of(agent, '你好')
    assert reasons[-1] == 'done', f'应该正常结束，实际 {reasons}'
    assert llm.calls >= 2, '第一次失败、第二次成功，至少调用两次'


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
