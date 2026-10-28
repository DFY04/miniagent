"""MCP 接入 demo —— 定义 → 挂载 → 调用，全程本地（不联网、不花钱）。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.mcp.demo

它跑的是四步：
    ① 起一个真的 MCP server 子进程（miniagent/mcp/fake_server.py）
    ② 把它的工具挂到 Agent 上（tools=[...]）
    ③ 跑一句提问，看 Agent 怎么调远程工具
    ④ 退出 with，子进程被关掉

★ 这里用的是【假 LLM】：真模型的版本见 examples/tool_mount_real.py，
  只需要把下面 OneShotLLM 换成 LLMClient({'model': 'qwen-max'})。
"""
from __future__ import annotations

import sys

from miniagent.core.agent import Agent
from miniagent.core.message import FunctionCall, Message
from miniagent.mcp import MCPClient


class OneShotLLM:
    """假装是模型：第一圈请求调某个工具，第二圈收工。"""

    def __init__(self, tool_name: str, arguments: str = '{}'):
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            names = [t['name'] for t in (tools or [])]
            print(f'    [LLM] 收到 {len(names)} 个工具说明书：{names}')
            print(f'    [LLM] 决定调用 {self.tool_name}，参数 {self.arguments}')
            return Message.assistant(function_call=FunctionCall(
                name=self.tool_name, arguments=self.arguments))
        print(f'    [LLM] 收到工具结果：{messages[-1].content!r}')
        return Message.assistant('好的，处理完了')


def hr(title: str) -> None:
    print('\n' + '=' * 70)
    print(f'  {title}')
    print('=' * 70)


def main() -> None:
    print('=' * 70)
    print('  MCP 接入 demo（本地 fake server，不需要网络）')
    print('=' * 70)

    # ① 起 server：它就是 miniagent/mcp/fake_server.py 里的 MCPServer
    with MCPClient.stdio(sys.executable,
                         ['-m', 'miniagent.mcp.fake_server'],
                         cwd='.') as mcp:

        hr('① server 提供了哪些工具（定义在 fake_server.py 里）')
        for t in mcp.tools:
            params = list(t.parameters.get('properties', {}))
            print(f'  {t.name:<8} 参数={params or "（无）"}')
            print(f'           desc: {t.description}')

        hr('② 挂到 Agent 上（这一行就是全部接入工作）')
        agent = Agent(llm=OneShotLLM('add', '{"a": 37, "b": 58}'), tools=mcp.tools)
        print(f'  agent.tool_map = {list(agent.tool_map)}')
        print('  （核心 core/agent.py 一行都没改）')

        hr('③ 跑一句提问')
        print('    提问：37 + 58 等于多少？')
        for m in agent.run([Message.user('37 + 58 等于多少？')]):
            if m.function_call:
                print(f'    [{m.step}步 assistant] 请求调 {m.function_call.name}({m.function_call.arguments})')
            elif m.role == 'function':
                print(f'    [{m.step}步 function ] {m.name} 返回 {m.content!r}')
            else:
                print(f'    [{m.step}步 assistant] 最终答案：{m.content}')

        hr('④ 远程工具报错会怎样（不会炸主循环）')
        agent2 = Agent(llm=OneShotLLM('boom'), tools=mcp.tools)
        for m in agent2.run([Message.user('试一下 boom')]):
            if m.role == 'function':
                print(f'    function 消息：{m.content}')

    print('\n' + '=' * 70)
    print('  出了 with 块，server 子进程已经被关掉了')
    print(f'  {mcp}')
    print('=' * 70)


if __name__ == '__main__':
    main()
