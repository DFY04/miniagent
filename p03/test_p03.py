"""P03 验收测试：摘要压缩。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/p03/test_p03.py -v
    .venv\\Scripts\\python.exe -m miniagent.p03.test_p03

测试里 `estimate_tokens` 会被换成「1 字符 = 1 token」，方便算预算。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core import context  # noqa: E402
from miniagent.core.message import Message  # noqa: E402


def _use_char_counter():
    context.estimate_tokens = lambda text: len(text or '')


class FakeSummaryLLM:
    """假装会做摘要的 LLM，返回固定的摘要文本。"""

    def __init__(self, summary: str = '【摘要】用户叫小明，想去杭州。'):
        self.summary = summary
        self.calls = 0
        self.last_prompt = None

    def chat(self, messages, tools=None):
        self.calls += 1
        self.last_prompt = messages[-1].content
        return Message.assistant(self.summary)


class BoomLLM:
    """一调用就炸的 LLM，用来测降级。"""

    def chat(self, messages, tools=None):
        raise RuntimeError('LLM 挂了')


def _big_conversation() -> list[Message]:
    return [
        Message.system('你是一个助手'),
        Message.user('我叫小明，' + '废话' * 100),
        Message.assistant('好的小明，' + '废话' * 100),
        Message.user('我想去杭州'),
    ]


# ==================== summarize ====================

def test_summarize_returns_llm_content():
    llm = FakeSummaryLLM('这是摘要')
    assert context.summarize([Message.user('随便什么')], llm) == '这是摘要'


def test_summarize_puts_dialogue_into_prompt():
    """★ 原对话内容必须出现在 prompt 里，否则 LLM 无从摘要。"""
    llm = FakeSummaryLLM()
    context.summarize([Message.user('我叫小明'), Message.assistant('你好')], llm)

    assert llm.calls == 1
    assert '我叫小明' in llm.last_prompt
    assert '你好' in llm.last_prompt


def test_summarize_prompt_asks_to_keep_facts():
    """prompt 里要交代"保留关键事实"。"""
    llm = FakeSummaryLLM()
    context.summarize([Message.user('x')], llm)
    prompt = llm.last_prompt
    assert ('事实' in prompt) or ('关键' in prompt)


# ==================== compress: 没超预算 ====================

def test_compress_short_returns_unchanged():
    llm = FakeSummaryLLM()
    msgs = [Message.system('s'), Message.user('hi')]
    out = context.compress(msgs, llm, 1000)

    assert out == msgs
    assert llm.calls == 0, '★ 没超预算就不该调 LLM'


# ==================== compress: 超预算 ====================

def test_compress_summarizes_the_dropped_part():
    _use_char_counter()
    llm = FakeSummaryLLM('用户说他叫小明，想去杭州。')
    msgs = _big_conversation()

    out = context.compress(msgs, llm, 100)

    assert llm.calls == 1, '★ 超预算了，应该调一次摘要'

    text = '\n'.join(m.content or '' for m in out)
    assert '小明' in text, '★ 摘要里的关键事实要保住'
    assert '想去杭州' in text or '杭州' in text


def test_compress_summary_sits_after_system():
    _use_char_counter()
    llm = FakeSummaryLLM('摘要内容')
    out = context.compress(_big_conversation(), llm, 100)

    assert out[0].role == 'system', '第一条应该还是原 system'
    assert '摘要' in (out[1].content or ''), '★ 摘要要插在 system 后面'


def test_compress_keeps_last_user_message():
    _use_char_counter()
    llm = FakeSummaryLLM('摘要内容')
    out = context.compress(_big_conversation(), llm, 100)

    assert out[-1].content == '我想去杭州', '★ 最后一条 user 必须保住'


def test_compress_respects_budget():
    _use_char_counter()
    llm = FakeSummaryLLM('短摘要')
    out = context.compress(_big_conversation(), llm, 60)

    total = sum(len(m.content or '') for m in out)
    assert total <= 60, f'压完还超预算：{total}'


# ==================== compress: 降级 ====================

def test_compress_falls_back_when_llm_raises():
    """★ summarize 抛异常 → 降级成 truncate，绝不能崩。"""
    _use_char_counter()
    msgs = _big_conversation()
    out = context.compress(msgs, BoomLLM(), 100)

    assert out == context.truncate(msgs, 100)


def test_compress_falls_back_when_summary_empty():
    """LLM 返回空摘要 → 也降级。"""
    _use_char_counter()
    msgs = _big_conversation()
    out = context.compress(msgs, FakeSummaryLLM(''), 100)

    assert out == context.truncate(msgs, 100)


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
