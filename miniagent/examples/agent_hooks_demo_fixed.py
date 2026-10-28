"""P01 demo（修好版）—— 事件总线 + 挂了工具的 Agent + 审批闸门。

注意：这个文件和你原来那份 agent_hooks_demo.py 是两份，
     你原来那份我没再动。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.agent_hooks_demo_fixed

三个订阅者演示了三种不同的"介入程度"：
    tracer    → 只看一眼，记录
    approval  → 要插手，危险操作前拦住（靠 raise ApprovalRejected）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

from miniagent.core.hooks import ApprovalRejected, EventBus, EventType
from miniagent.core.hooks_agent import Agent          # ★ 带埋点的 Agent
from miniagent.core.message import Message
from miniagent.llm import LLMClient
from miniagent.tools import demo_tools  # noqa: F401  import 一下，now/add 才会注册


# 危险工具名单（命中就要人工审批）
DANGEROUS = {'add'}          # 演示用：把 add 当成危险操作


# ==================== 订阅者 ====================

def tracer(**kw):
    """只看一眼，记录。不影响流程。"""
    print(f'  [tracer] {kw}')


def approval(**kw):
    """要插手：危险操作前问人工。拒绝就 raise，让 Agent 中断。

    ★ 这里抛的 ApprovalRejected 会穿过总线（总线对它不隔离），
      一路冒到 Agent.run() 的 except 里。
    """
    name = kw.get('name')
    if name not in DANGEROUS:
        print(f'  [approval] {name} 不在危险名单里，放行')
        return

    answer = input(f'  [approval] {name} 是危险操作，批准吗？(y/n) ')
    if answer.strip().lower() != 'y':
        print(f'  [approval] 拒绝 {name} → raise ApprovalRejected')
        raise ApprovalRejected(name)
    print(f'  [approval] 已批准 {name}')


def main() -> None:
    load_dotenv()

    # ---- 1) 建总线，注册订阅者 ----
    bus = EventBus()
    bus.subscribe_all(tracer)                          # 所有事件都记一笔
    bus.subscribe(EventType.TOOL_START, approval)      # 只在"要调工具"时插手

    # ---- 2) 建 Agent，挂上工具，并把 bus 传进去 ----
    llm = LLMClient({'model': 'qwen-max'})
    agent = Agent(
        llm=llm,
        system_message='你是一个智能助手',
        tools=['now', 'add'],
        max_steps=10,
        bus=bus,                                       # ★ 传了 bus 才会发事件
    )

    # ---- 3) 跑 ----
    question = sys.argv[1] if len(sys.argv) > 1 else '帮我算一下 1+2，再看看现在几点'
    print(f'用户: {question}\n')
    for msg in agent.run([Message.user(question)]):
        print(f'  [yield] {msg}')


if __name__ == '__main__':
    main()
