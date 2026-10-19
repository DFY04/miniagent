"""工具是怎么"挂载"到 Agent 上的 —— 可运行的检查例子。

跑法（项目根目录）：
    python -m miniagent.examples.tool_mount_demo

════════════════════════════════════════════════════════════════════
【一句话】
    "挂载" = 把工具从【全局目录】取出来，放进【这个 Agent 自己的口袋】，
             然后把它的"说明书"交给模型。

    两级注册表：
        第一级  TOOL_REGISTRY  名字 -> 【类】    全局，@tool 装饰器填的
        第二级  agent.tool_map 名字 -> 【实例】  每个 Agent 一份，构造时填的
════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
from typing import Union

from miniagent.core.agent import Agent
from miniagent.core.message import FunctionCall, Message
from miniagent.core.tool import TOOL_REGISTRY, BaseTool, tool
from miniagent.tools import demo_tools  # noqa: F401  ★ import 这一下就完成了 'now'/'add' 的注册


def hr(title: str) -> None:
    print('\n' + '=' * 72)
    print(f'  {title}')
    print('=' * 72)


# ============================================================================
#  定义三个工具（演示三种情况）
# ============================================================================

@tool('echo')
class EchoTool(BaseTool):
    """带 cfg 的工具：同一个类，可以造出行为不同的实例。"""
    description = '把输入的文本加上前缀后原样返回。'
    parameters = {
        'type': 'object',
        'properties': {'text': {'type': 'string', 'description': '要回显的文本'}},
        'required': ['text'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        return f'{self.cfg.get("prefix", "")}{p["text"]}'


@tool('boom')
class BoomTool(BaseTool):
    """故意抛异常的工具：验证"工具炸了不会炸主循环"。"""
    description = '永远失败（演示用）。'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params: Union[str, dict], **kwargs) -> str:
        raise RuntimeError('我炸了')


class NotDecoratedTool(BaseTool):
    """★ 故意不加 @tool：它【不会】进 TOOL_REGISTRY。"""
    description = '没被注册的工具。'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params, **kwargs) -> str:
        return 'ok'


# ============================================================================
#  假 LLM：第一圈请求调工具，第二圈收工（并打印它收到了什么说明书）
# ============================================================================

class OneShotLLM:
    def __init__(self, tool_name: str, arguments: str = '{}'):
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            names = [t['name'] for t in (tools or [])]
            print(f'    [LLM 第 1 圈] 我收到了 {len(names)} 个工具的说明书：{names}')
            print(f'    [LLM 第 1 圈] 决定调用 `{self.tool_name}`，参数 {self.arguments}')
            return Message.assistant(function_call=FunctionCall(
                name=self.tool_name, arguments=self.arguments))
        last = messages[-1]
        print(f'    [LLM 第 2 圈] 收到工具结果（role={last.role}）：{last.content!r}')
        return Message.assistant('搞定')


# ============================================================================
#  主流程
# ============================================================================

def main() -> None:
    hr('第 1 步：@tool 装饰器把【类】登记进全局表（第一级）')
    print('  TOOL_REGISTRY 里的名字：')
    for name, cls in sorted(TOOL_REGISTRY.items()):
        print(f'    {name:<8} -> {cls.__name__}  （类对象，不是实例）')

    hr('第 2 步：没被 @tool 装饰的类，不在全局表里')
    print(f'  NotDecoratedTool 在表里吗？',
          'NotDecoratedTool' in TOOL_REGISTRY or '不在（因为它没被 @tool 标记）')
    print(f'  表里一共有 {len(TOOL_REGISTRY)} 个工具')

    hr('第 3 步：Agent 构造时"挂载" -> 生成 tool_map（第二级）')
    print('  三种写法都能挂：\n')
    print("    agent = Agent(llm=..., tools=['now'])            # ① 传名字")
    print("    agent = Agent(llm=..., tools=[EchoTool()])       # ② 传实例")
    print("    agent = Agent(llm=..., tools=[{'name': 'echo',   # ③ 传配置 dict")
    print("                                'prefix': 'X:'}])")

    llm1 = OneShotLLM('now')
    agent_a = Agent(llm=llm1, tools=['now'])                       # ① 名字
    agent_b = Agent(llm=OneShotLLM('echo'), tools=[EchoTool()])    # ② 实例
    agent_c = Agent(llm=OneShotLLM('echo'),
                    tools=[{'name': 'echo', 'prefix': 'C:'}])      # ③ 配置 dict

    print('\n  agent_a.tool_map：', {k: type(v).__name__ for k, v in agent_a.tool_map.items()})
    print('  agent_b.tool_map：', {k: type(v).__name__ for k, v in agent_b.tool_map.items()})
    print('  agent_c.tool_map：', {k: type(v).__name__ for k, v in agent_c.tool_map.items()})
    print('\n  ★ 同一个 `echo`：')
    print('      全局表里的类是同一个  :', TOOL_REGISTRY["echo"])
    print('      agent_b 的实例        :', agent_b.tool_map["echo"])
    print('      agent_c 的实例        :', agent_c.tool_map["echo"])
    print('      两者是同一个对象吗？  :',
          agent_b.tool_map["echo"] is agent_c.tool_map["echo"], '（不同，因为各自 new 了一次）')
    print('      agent_c 实例的 cfg    :', agent_c.tool_map["echo"].cfg)

    hr('第 4 步：tool_schemas —— 真正"交给模型"的东西')
    print(json.dumps(agent_a.tool_schemas, ensure_ascii=False, indent=2))

    hr('第 5 步：跑一遍，看工具是怎么被调用的')
    print('  --- agent_a（now）---')
    print('  产出：', [m.content or f'调{m.function_call.name}' for m in agent_a.run([Message.user('现在几点')])])

    print('\n  --- agent_c（echo，cfg.prefix="C:"）---')
    llm_c = OneShotLLM('echo', '{"text": "hello"}')
    agent_c = Agent(llm=llm_c, tools=[{'name': 'echo', 'prefix': 'C:'}])
    out = list(agent_c.run([Message.user('回显 hello')]))
    print('  产出：', [(m.role, m.content) for m in out])

    hr('第 6 步：名字写错 / 没注册 -> 挂载时就报错')
    try:
        Agent(llm=OneShotLLM('now'), tools=['not_registered'])
    except ValueError as exc:
        print('  ', exc)

    hr('第 7 步：工具抛异常 -> 变成 observation 回给模型，主循环不炸')
    llm_b = OneShotLLM('boom')
    agent_boom = Agent(llm=llm_b, tools=['boom'])
    out = list(agent_boom.run([Message.user('试一下')]))
    for m in out:
        if m.role == 'function':
            print(f'  function 消息（工具结果）：{m.content}')

    hr('小结')
    print('  挂载 = ① 从 TOOL_REGISTRY 按名字取【类】')
    print('         ② 用 cfg 实例化成【对象】')
    print('         ③ 放进 agent.tool_map（这个 Agent 的口袋）')
    print('         ④ 把 tool_schemas 交给模型，模型才知道有这个工具')
    print()
    print('  ★ 第一级全局共享（类），第二级各自一份（实例）——')
    print('    所以两个 Agent 用同名工具、不同配置，互不影响。')


if __name__ == '__main__':
    main()
