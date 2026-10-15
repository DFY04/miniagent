"""P04 验收测试：短期记忆（长期记忆见 tests/test_rag_memory.md 的说明）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/p04/test_p04.py -v
    .venv\\Scripts\\python.exe -m miniagent.p04.test_p04

【为什么这里只测短期记忆】
    长期记忆最终选的是"自建 RAG"路线（不走 qwen-agent 的 Retrieval，
    也不包一个 LongTermMemory 门面类），所以没有可离线测的类：
        memory/vector_store.py   DashScopeChromaRAG（要联网 + 要 API KEY）
        memory/embedders.py      HashEmbedder（离线玩具版）/ DashScopeEmbedder
    长期记忆的验收靠"实跑"：
        .venv\\Scripts\\python.exe -m miniagent.examples.agent_hooks_demo
    看它能不能把 asn.txt 里的内容检索出来并回答。

    短期记忆仍然全离线测：ShortTermMemory(max_tokens, llm=假LLM)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core import context  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402


def _use_char_counter():
    """把 estimate_tokens 换成「1 字符 = 1 token」，预算好算。"""
    context.estimate_tokens = lambda text: len(text or '')


class FakeSummaryLLM:
    """假 LLM：返回固定摘要，并记录被调了几次。"""

    def __init__(self, summary: str = '用户说他很累'):
        self.summary = summary
        self.calls = 0
        self.last_prompt = None

    def chat(self, messages, tools=None):
        self.calls += 1
        self.last_prompt = messages[-1].content
        return Message.assistant(self.summary)


# ==================== ShortTermMemory ====================

def test_stm_returns_all_when_under_budget():
    """没超预算 → 原样返回，顺序不变。"""
    mem = ShortTermMemory(max_tokens=1000)
    mem.add(Message.system('你是助手'))
    mem.add(Message.user('你好'))
    out = mem.build()
    assert [m.role for m in out] == ['system', 'user']


def test_stm_build_respects_budget():
    """★ 超预算 → build() 必须裁到预算内。"""
    _use_char_counter()
    mem = ShortTermMemory(max_tokens=50)
    for _ in range(10):
        mem.add(Message.assistant('x' * 100))
    mem.add(Message.user('最后的问题'))

    out = mem.build()
    total = sum(len(m.content or '') for m in out)
    assert total <= 50, f'还是超了：{total}'


def test_stm_build_keeps_last_user():
    """★ 裁的时候，最后一条 user 必须保住。"""
    _use_char_counter()
    mem = ShortTermMemory(max_tokens=50)
    for _ in range(10):
        mem.add(Message.assistant('x' * 100))
    mem.add(Message.user('最后的问题'))

    out = mem.build()
    assert out[-1].content == '最后的问题'


def test_stm_uses_llm_when_over_budget():
    """★ 超预算 + 有 llm → 应该调 LLM 做摘要（而不是直接丢）。"""
    _use_char_counter()
    llm = FakeSummaryLLM('用户说他很累')
    mem = ShortTermMemory(max_tokens=50, llm=llm)
    mem.add(Message.system('sys'))
    mem.add(Message.user('我很累' + '废话' * 100))
    mem.add(Message.user('最后的问题'))

    out = mem.build()
    assert llm.calls >= 1, '超预算了，应该调一次摘要'

    text = '\n'.join(m.content or '' for m in out)
    assert '很累' in text, '摘要里的信息要保住'


def test_stm_without_llm_still_works():
    """★ 没有 llm → 退化成"只裁不摘要"，不崩、也不超预算。"""
    _use_char_counter()
    mem = ShortTermMemory(max_tokens=50)          # 不给 llm
    mem.add(Message.system('sys'))
    mem.add(Message.user('x' * 500))
    mem.add(Message.user('最后'))

    out = mem.build()
    total = sum(len(m.content or '') for m in out)
    assert total <= 50
    assert out[-1].content == '最后'


def test_stm_add_does_not_mutate_input():
    """add 只是记录，不该改动传进来的消息。"""
    msg = Message.user('原始内容')
    mem = ShortTermMemory(max_tokens=1000)
    mem.add(msg)
    assert msg.content == '原始内容'
    assert mem.build()[0] is msg or mem.build()[0] == msg


# ==================== LongTermMemory（已废弃，见文件头说明）====================
# 原来有 5 条用例测 LongTermMemory。长期记忆改成自建 RAG 后，
# 那个门面类连同 memory/retriever.py 一起删了，这里的用例也一并移除。
# 长期记忆的验收方式：跑 examples/agent_hooks_demo.py，看能不能答出 asn.txt 里的内容。


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
