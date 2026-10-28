"""test_p02_context_truncate.py —— core/context.truncate 的回归测试（全离线）。

【这条测试固化的是什么洞】
    旧实现假设 `messages[-1]` 就是“最后一条 user”：

        last_user_token = estimate_tokens(messages[-1].content)
        ...
        new_list1.append(messages[-1])

    但 mem_agent.build() 交给模型的上下文，最后一条常常是 function
    （user -> assistant(tool_call) -> function），也可能是 assistant 的最终回答。
    这时旧实现会：
        ① 把工具结果/回答的 token 当成 user 的 token 记账（预算算错）；
        ② 在预算紧张时把【用户真正的问题】裁掉，只留下断头的工具结果。

【修法】从后往前找最后一条 user；它之后的 assistant(tool_call)+function
整段一起保留（同一轮 ReAct 的一问一答不能拆）。找不到 user 时保守处理。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p02_context_truncate.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core import context  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402


def _tool_tail_conversation() -> list[Message]:
    """模拟 mem_agent.build() 的典型上下文：以 function（工具结果）结尾。"""
    return [
        Message.system('sys'),
        Message.user('x' * 200),          # ★ 很长的问题：预算紧时旧实现会把它裁掉
        Message.assistant('', function_call=FunctionCall('write_file', '{"path": "a.txt"}')),
        Message.function('write_file', 'ok'),
    ]


# ==================== 最后一条不是 user ====================

def test_truncate_finds_last_user_when_tail_is_function():
    """★ 回归：最后一条是 function 时，真正的最后一条 user 不能被裁掉。"""
    out = context.truncate(_tool_tail_conversation(), 30)

    assert any(m.role == 'user' for m in out), \
        '用户的问题被裁掉了（是不是还在拿 messages[-1] 当最后一条 user？）'
    assert out[0].role == 'system'


def test_truncate_keeps_user_and_tool_tail_as_one_round():
    """最后一条是 function → user + assistant(tool_call) + function 要整段保留。

    只留 user、把工具结果丢掉，模型会重新再调一次工具；只留 function、把 user 丢掉，
    模型不知道自己要干什么 —— 两种断头上下文都会让 Agent 空转。
    """
    out = context.truncate(_tool_tail_conversation(), 30)

    assert [m.role for m in out] == ['system', 'user', 'assistant', 'function'], \
        f'上下文被裁成了断头状态：{[m.role for m in out]}'
    assert out[-2].function_call is not None, '工具结果前面的那条 assistant(tool_call) 丢了'


def test_truncate_finds_last_user_when_tail_is_assistant():
    """最后一条是 assistant 的最终回答时，同样不能把它当成 user。"""
    msgs = [
        Message.system('sys'),
        Message.user('y' * 200),
        Message.assistant('好的，已完成。'),
    ]
    out = context.truncate(msgs, 30)

    assert any(m.role == 'user' for m in out), '真正的 user 被当成了 assistant 裁掉'
    assert out[-1].role == 'assistant' and '已完成' in out[-1].content


# ==================== 找不到 user 的保守处理 ====================

def test_truncate_without_user_does_not_crash():
    """没有 user（只有 system + assistant）→ 不崩、不重复 system。"""
    msgs = [Message.system('sys'), Message.assistant('a' * 100)]
    out = context.truncate(msgs, 1)

    assert out[0].role == 'system'
    assert out[-1].role == 'assistant'


def test_truncate_only_system_does_not_duplicate():
    """只有 system 消息时，不能被复制成两条。"""
    msgs = [Message.system('sys')]
    out = context.truncate(msgs, 1000)
    assert [m.role for m in out] == ['system']


# ==================== 老行为不回归 ====================

def test_truncate_normal_case_still_keeps_last_user_last():
    """常规形态（最后一条就是 user）保持原语义：system 打头、最后一条 user 结尾。"""
    msgs = [
        Message.system('sys'),
        Message.user('旧问题'),
        Message.assistant('旧回答'),
        Message.user('新问题'),
    ]
    out = context.truncate(msgs, 1000)
    assert out[0].role == 'system'
    assert out[-1].content == '新问题'
    assert [m.role for m in out] == ['system', 'user', 'assistant', 'user']


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
