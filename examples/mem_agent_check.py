"""验证 mem_agent：记忆能不能跨轮生效。

跑法（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.mem_agent_check

用假 LLM，不联网不花钱。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.mem_agent import Agent  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402


class SpyLLM:
    """假 LLM：每次被调用都记下"它看到了几条消息"。"""

    def __init__(self):
        self.calls = 0
        self.seen_history = []          # 每次调用时收到的历史（只记 content）

    def chat(self, messages, tools=None):
        self.calls += 1
        self.seen_history.append([m.content for m in messages])
        return Message.assistant(f'回复第{self.calls}次')


def main() -> None:
    llm = SpyLLM()
    mem = ShortTermMemory(max_tokens=100000)          # 预算很大，不会触发压缩
    agent = Agent(llm=llm, system_message='你是一个数学助手', mem=mem)

    print('=== 第一轮：只传一条新消息 ===')
    for msg in agent.run([Message.user('我叫小明')]):
        print(f'  [yield] {msg.content}')

    print()
    print('=== 第二轮：再传一条新消息（不带历史）===')
    for msg in agent.run([Message.user('我叫什么？')]):
        print(f'  [yield] {msg.content}')

    print()
    print('=== 检查：第二轮模型看到的历史里，有没有第一轮的内容？===')
    for i, hist in enumerate(llm.seen_history, 1):
        print(f'  第{i}次调用看到 {len(hist)} 条: {hist}')

    last = llm.seen_history[-1]
    ok = ('我叫小明' in last) and ('我叫什么？' in last) and any('数学助手' in (h or '') for h in last)
    print()
    if ok:
        print('✅ 记忆生效：第二轮看到了 system + 第一轮的对话 + 本轮问题')
    else:
        print('❌ 记忆没生效 —— 检查 system 有没有进 mem、build() 有没有传错参数')


if __name__ == '__main__':
    main()
