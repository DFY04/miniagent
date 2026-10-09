"""P01 验收测试：事件总线。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p01_hooks.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p01_hooks
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import Event, EventBus, EventType  # noqa: E402


# ==================== 基础行为 ====================

def test_subscribers_called_in_registration_order():
    """订阅者按【注册顺序】被调用。"""
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('a'))
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('b'))
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('c'))
    bus.emit(EventType.LLM_END)
    assert calls == ['a', 'b', 'c']


def test_only_matching_event_is_notified():
    """只有订阅了该事件的才被通知。"""
    bus = EventBus()
    calls = []
    bus.subscribe(EventType.LLM_START, lambda **kw: calls.append('start'))
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('end'))
    bus.emit(EventType.LLM_END)
    assert calls == ['end']


def test_event_data_is_passed_to_handler():
    """emit 传的数据能到订阅者手里。"""
    bus = EventBus()
    got = {}
    bus.subscribe(EventType.TOOL_START, lambda **kw: got.update(kw))
    bus.emit(EventType.TOOL_START, name='interg')
    assert got == {'name': 'interg'}


def test_subscribe_all_receives_every_event():
    """通配订阅者收到所有事件。"""
    bus = EventBus()
    seen = []
    bus.subscribe_all(lambda **kw: seen.append('all'))
    bus.emit(EventType.LLM_START)
    bus.emit(EventType.RUN_END)
    assert seen == ['all', 'all']


# ==================== 异常隔离（P01 核心验收）====================

def test_exception_in_subscriber_does_not_break_others():
    """★ 验收：一个订阅者抛异常，其他订阅者【照常收到】。"""
    bus = EventBus()
    calls = []

    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('first'))

    def boom(**kw):
        raise RuntimeError('我崩了')

    bus.subscribe(EventType.LLM_END, boom)
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('after_boom'))

    bus.emit(EventType.LLM_END)
    assert calls == ['first', 'after_boom']


def test_exception_in_subscribe_all_is_also_isolated():
    """★ 验收：通配订阅者抛异常，也不能影响别人。"""
    bus = EventBus()
    calls = []

    def boom(**kw):
        raise RuntimeError('通配的崩了')

    bus.subscribe_all(boom)
    bus.subscribe(EventType.LLM_END, lambda **kw: calls.append('normal'))

    bus.emit(EventType.LLM_END)
    assert calls == ['normal']


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
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
