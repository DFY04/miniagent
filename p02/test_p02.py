"""P02 验收测试：上下文截断。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/p02/test_p02.py -v
    .venv\\Scripts\\python.exe -m miniagent.p02.test_p02

说明：测试会把 estimate_tokens 替换成「1 字符 = 1 token」，
      这样预算算起来是确定的，不依赖你估算得多准。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.p02 import context  # noqa: E402


def _use_char_counter():
    """把 estimate_tokens 换成 1 字符=1 token，测试好算。"""
    context.estimate_tokens = lambda text: len(text or '')


# ==================== estimate_tokens ====================

def test_estimate_tokens_empty_is_zero():
    assert context.estimate_tokens('') == 0


def test_estimate_tokens_grows_with_text():
    """文本越长，估出来的 token 越多（不要求精确）。"""
    assert context.estimate_tokens('a' * 400) > context.estimate_tokens('a' * 40)


# ==================== truncate: 基本 ====================

def test_short_conversation_is_untouched():
    """没超预算就原样返回。"""
    msgs = [Message.system('s'), Message.user('hi'), Message.assistant('yo')]
    assert context.truncate(msgs, 1000) == msgs


def test_returns_new_list_and_does_not_mutate_input():
    msgs = [Message.system('s'), Message.user('hi')]
    out = context.truncate(msgs, 1000)
    assert out is not msgs
    assert len(msgs) == 2


# ==================== truncate: 保底规则 ====================

def test_system_and_last_user_survive_even_over_budget():
    """★ 规则 1+2+5：预算再小，system 和最后一条 user 都得在。"""
    _use_char_counter()
    msgs = [
        Message.system('s'),
        Message.user('x' * 100),
        Message.assistant('y' * 100),
        Message.user('z' * 100),
    ]
    out = context.truncate(msgs, 10)          # 远远不够
    assert out[0].role == 'system'
    assert out[-1].content == 'z' * 100


# ==================== truncate: 删最老的 ====================

def test_oldest_dropped_first():
    """★ 规则 3：从最老开始删。"""
    _use_char_counter()
    msgs = [
        Message.system('s'),                  # 1
        Message.user('O' * 200),              # 200  ← 最老，应该被删
        Message.assistant('M' * 200),         # 200
        Message.user('L' * 40),               # 40   ← 最后一条 user，必留
    ]
    out = context.truncate(msgs, 250)
    contents = [m.content for m in out]
    assert 'O' * 200 not in contents, '最老的那条应该被删掉'
    assert 'L' * 40 in contents


def test_kept_budget_is_respected():
    """裁完的总 token 不超过预算（除非只剩保底那两条）。"""
    _use_char_counter()
    msgs = [Message.system('s')] + [Message.assistant('a' * 100) for _ in range(10)] + [Message.user('q')]
    out = context.truncate(msgs, 300)
    total = sum(len(m.content or '') for m in out)
    assert total <= 300


# ==================== truncate: 孤立 function ====================

def test_no_orphan_function_message():
    """★ 规则 4：function 消息前面必须还有它的 assistant。"""
    _use_char_counter()
    msgs = [
        Message.system('s'),                                        # 1
        Message.assistant('A' * 100, function_call=FunctionCall('t', '{}')),   # 100
        Message.function('t', 'r'),                                 # 1  ← 便宜，会被选中
        Message.user('q'),                                          # 1  ← 最后一条 user
    ]
    # 预算 20：system(1) + user(1) + function(1) 塞得下，但那条 100 的 assistant 塞不下
    out = context.truncate(msgs, 20)

    for i, m in enumerate(out):
        if m.role == 'function':
            assert i > 0 and out[i - 1].role == 'assistant', '出现了孤立的 function 消息'


def test_normal_tool_pair_is_kept_together():
    """预算够的时候，assistant(function_call) + function 应该都在。"""
    _use_char_counter()
    msgs = [
        Message.system('s'),
        Message.assistant('a' * 10, function_call=FunctionCall('t', '{}')),
        Message.function('t', 'b' * 10),
        Message.user('q'),
    ]
    out = context.truncate(msgs, 1000)
    roles = [m.role for m in out]
    assert roles == ['system', 'assistant', 'function', 'user']


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
