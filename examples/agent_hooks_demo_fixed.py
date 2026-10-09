"""P01 demo（修好版）—— 事件总线 + 挂了工具的 Agent。

注意：这个文件和你原来那份 agent_hooks_demo.py 是两份，
     你原来那份我没再动。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.agent_hooks_demo_fixed

修了原版的 6 个问题：
  ① from build.lib.qwen_agent.llm import ...   → 删掉，改用 LLMClient
  ② 重复定义 EventType                          → 删掉，用 import 的
  ③ bus.subscribe(..., llmstart())              → 去掉括号
  ④ llm = get_chat_model = {...}                → 改成 LLMClient({'model': 'qwen-max'})
  ⑤ Agent(llm) 传的是 dict                      → 同上
  ⑥ DF_Agent.run("现在几点了")                   → 传 List[Message] 且要 for 迭代
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass
class Event:
    type: EventType
    data: dict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

from miniagent.core.agent import Agent
from miniagent.core.hooks import EventBus, EventType
from miniagent.core.message import Message
from miniagent.llm import LLMClient
from miniagent.tools import demo_tools  # noqa: F401  import 一下，now/add 才会注册


# ==================== 订阅者 ====================
# 统一用 **kw：emit 传什么参数进来都不会因为签名对不上而报错
def llm_start(**kw):
    print('  [hook] 开始调用大模型了')


def tool_start(**kw):
    print(f'  [hook] 开始执行工具了: {kw.get("name")}')


def run_end(**kw):
    print('  [hook] 任务结束')


def main() -> None:
    load_dotenv()

    # ---- 1) 建总线，注册订阅者 ----
    bus = EventBus()
    bus.subscribe(EventType.LLM_START, llm_start)      # ★ 传函数本身，不带括号
    bus.subscribe(EventType.TOOL_START, tool_start)
    bus.subscribe(EventType.RUN_END, run_end)

    # ---- 2) 建 Agent，挂上两个工具 ----
    llm = LLMClient({'model': 'qwen-max'})
    agent = Agent(
        llm=llm,
        system_message='你是一个智能助手',
        tools=['now', 'add'],
        max_steps=10,
    )

    # ---- 3) 跑 ----
    # 说明：Agent 目前还没把 bus 接进去（那是下一步），所以这里手动 emit
    bus.emit(EventType.LLM_START)
    for msg in agent.run([Message.user('现在几点了')]):
        if msg.function_call:
            bus.emit(EventType.TOOL_START, name=msg.function_call.name)
        print(msg)
    bus.emit(EventType.RUN_END)


if __name__ == '__main__':
    main()
