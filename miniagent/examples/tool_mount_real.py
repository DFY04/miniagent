"""工具挂载的【真模型】演示 —— 用 qwen-max 真跑一遍，看完整的挂载→调用流程。

跑法（项目根目录，需要 .env 里有 DASHSCOPE_API_KEY）：
    .venv\\Scripts\\python.exe -m miniagent.examples.tool_mount_real

它会跑两轮对比：
    A. 挂上工具  -> 模型看到说明书，会主动 function_call 调用工具
    B. 撤掉工具  -> 同一句提问，模型只能老实说"我不知道现在几点"

★ 这个对比就是"挂载"的意义：挂载 = 把工具说明书交给模型。
  模型本身不会查时间，它只是"知道有这个工具"并且"按格式要求调用"。

★ 你会看到一件很重要的事：模型有时会【不调工具、直接凭记忆编一个时间】。
  这不是 bug，是 LLM 的固有行为 —— 也正是"评测"存在的理由：
  你只能统计"10 次里它乖乖调工具了几次"，无法用 assert 保证。
"""
from __future__ import annotations

import json
import sys

from miniagent.core.agent import Agent
from miniagent.core.message import Message
from miniagent.llm import LLMClient
from miniagent.tools import demo_tools  # noqa: F401  ★ import 一下，'now'/'add' 才会被注册

QUESTION = '现在几点了？请用工具查一下当前时间；然后再用工具帮我算 37 + 58。'

def show_schemas(schemas) -> None:
    print(f'  · 这次要发给模型的工具说明书（{len(schemas)} 个）：')
    for s in schemas:
        params = list(s['parameters'].get('properties', {}))
        print(f"      - {s['name']:<6} 参数={params or '（无）'}")
        print(f"        desc: {s['description'][:56]}...")


def run_round(title: str, tools) -> None:
    print('\n' + '=' * 74)
    print(f'  {title}')
    print('=' * 74)

    # ① 建模型（真调 DashScope）
    llm = LLMClient({'model': 'qwen-max'})

    # ② 建 Agent：这一步就是"挂载"
    agent = Agent(llm=llm, tools=tools, max_steps=6)

    # ③ 挂载结果：tool_map（实例）+ tool_schemas（说明书）
    print(f'  · agent.tool_map = {list(agent.tool_map)}')
    show_schemas(agent.tool_schemas)

    # ④ 真跑：这里会真的请求模型，可能好几圈
    print(f'\n  · 提问：{QUESTION}')
    history = [Message.user(QUESTION)]
    print('\n  · 主循环逐圈输出：')

    produced = list(agent.run(history))     # ★ 必须接住 yield，不能指望 history 被改
    for m in produced:
        if m.role == 'assistant' and m.function_call:
            fc = m.function_call
            print(f'    [第 {m.step} 步] assistant 想调工具 {fc.name}({fc.arguments})')
        elif m.role == 'function':
            print(f'    [第 {m.step} 步] function 返回：{m.content}')
        else:
            print(f'    [第 {m.step} 步] assistant 最终答案：{m.content}')

    called = [m.function_call.name for m in produced if m.function_call]
    if not called:
        print('    [!] 注意：这次模型【一个工具都没调】，上面的答案可能是它凭记忆编的！')
    else:
        print(f'    [OK] 本次调用了：{called}')

    # ⑤ 把完整的消息序列打出来（这才是上下文真实长什么样）
    #    ★ Agent.run 会 list(messages) 复制一份，所以调用方的 history 不会被改；
    #      要看完整上下文，得把“输入 + 产出”拼起来。
    full = list(history) + produced
    print('\n  · 这次 run 的完整上下文（输入 + 产出）：')
    for i, m in enumerate(full):
        tag = f'step={m.step}' if m.step is not None else 'step=-'
        extra = f' fc={m.function_call.name}' if m.function_call else ''
        name = f' name={m.name}' if m.name else ''
        print(f'    [{i}] {m.role:<9} {tag:<8}{name}{extra}  {(m.content or "")[:60]!r}')


def main() -> None:
    print('=' * 74)
    print('  工具挂载 · 真模型演示（qwen-max）')
    print('=' * 74)

    # ---- A：挂上工具 ----
    run_round('A. 挂上工具（tools=[now, add]）', tools=['now', 'add'])

    # ---- B：撤掉工具 ----
    run_round('B. 撤掉工具（tools=[]）—— 同样的提问', tools=[])

    print('\n' + '=' * 74)
    print('  对比结论')
    print('=' * 74)
    print('  A 轮：模型收到 2 个工具说明书 -> 主动发 function_call -> 拿到真实时间/计算结果')
    print('  B 轮：模型没收到任何说明书 -> 只能回答"我无法获取当前时间"')
    print()
    print('  ★ 模型自己不会查时间、不会算数（至少不可靠）；')
    print('    是"挂载"这一步把能力交给了它：')
    print('        挂载 = 说明书进 tools 参数 + 模型返回 function_call + Agent 执行 + 结果回灌')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
