"""P01 demo：单独验证事件总线（不需要 Agent、不需要 LLM）。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.agent_hook_demo

验证 P01 的两条验收标准：
  1. 订阅者能**按顺序**收到事件
  2. 一个订阅者抛异常，**不影响**其他订阅者
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import EventBus, EventType  # noqa: E402


# ==================== 订阅者（都是普通函数）====================
def tracer_llm_start(step):
    print(f'  [tracer]  第 {step} 步，开始问模型')


def tracer_llm_end(reply):
    print(f'  [tracer]  模型回话：{reply}')


def budget_llm_end(reply):
    print(f'  [budget]  记账（关心 llm_end）')


def approval_tool_start(name):
    print(f'  [approval] 检查工具：{name}')


def buggy_handler(reply):
    """故意崩，用来验证异常隔离。"""
    raise RuntimeError('我故意报错')


def main() -> None:
    bus = EventBus()

    print('=' * 60)
    print('场景 1：按注册顺序收到事件')
    print('=' * 60)
    bus.subscribe(EventType.LLM_START, tracer_llm_start)
    bus.subscribe(EventType.LLM_END, tracer_llm_end)
    bus.subscribe(EventType.LLM_END, budget_llm_end)
    bus.subscribe(EventType.TOOL_START, approval_tool_start)

    print('\n>> emit(LLM_START, step=0)')
    bus.emit(EventType.LLM_START, step=0)

    print('\n>> emit(LLM_END, reply="e的x次幂的不定积分是 e**x + C")')
    bus.emit(EventType.LLM_END, reply='e的x次幂的不定积分是 e**x + C')

    print('\n>> emit(TOOL_START, name="interg")')
    bus.emit(EventType.TOOL_START, name='interg')

    print()
    print('=' * 60)
    print('场景 2：异常隔离（中间插一个会崩的订阅者）')
    print('=' * 60)
    bus2 = EventBus()
    bus2.subscribe(EventType.LLM_END, tracer_llm_end)     # 第 1 个
    bus2.subscribe(EventType.LLM_END, buggy_handler)      # 第 2 个：崩
    bus2.subscribe(EventType.LLM_END, budget_llm_end)     # 第 3 个：应该照样收到

    print('\n>> emit(LLM_END)   （注意第 3 个订阅者还会不会跑）')
    bus2.emit(EventType.LLM_END, reply='你好')

    print()
    print('=' * 60)
    print('场景 3：subscribe_all —— 通配订阅所有事件')
    print('=' * 60)
    bus3 = EventBus()
    bus3.subscribe_all(lambda **kw: print(f'  [logger] 收到一个事件（不关心是什么）'))
    bus3.subscribe_all(lambda **kw: print(f'  [audit]  我也记一笔'))

    print('\n>> emit(LLM_START, step=0)')
    bus3.emit(EventType.LLM_START, step=0)
    print('\n>> emit(RUN_END)   （没传任何 data，也不报错）')
    bus3.emit(EventType.RUN_END)


if __name__ == '__main__':
    main()
