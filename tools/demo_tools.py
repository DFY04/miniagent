"""一个独立的示例工具：继承 BaseTool，用 @tool 注册。

为什么要单独一个文件？
  - 工具定义在哪个模块无所谓，关键是【那个模块要被 import 过】，
    否则 @tool 不会执行，TOOL_REGISTRY 里就没有它，挂载时会报"未注册"。

用法：
    from miniagent.tools import demo_tools   # ★ import 这一下就完成注册

    # 写法 A：传名字（Agent 帮你实例化）
    agent = Agent(llm=llm, tools=['now'])

    # 写法 B：自己实例化再传（要用到 cfg 时用这种）
    agent = Agent(llm=llm, tools=[demo_tools.NowTool()])
"""
from __future__ import annotations

import datetime
from typing import Union

from miniagent.core.tool import BaseTool, tool


@tool('now')
class NowTool(BaseTool):
    """返回当前的日期和时间。"""

    description = '获取当前的日期和时间。当用户问"现在几点""今天几号"之类的时调用。'

    # 没有任何参数
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params: Union[str, dict], **kwargs) -> str:
        now = datetime.datetime.now()
        return now.strftime('%Y-%m-%d %H:%M:%S')


@tool('add')
class AddTool(BaseTool):
    """两数相加（演示"带参数"的工具）。"""

    description = '计算两个数字的和。'
    parameters = {
        'type': 'object',
        'properties': {
            'a': {'type': 'number', 'description': '第一个加数'},
            'b': {'type': 'number', 'description': '第二个加数'},
        },
        'required': ['a', 'b'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        return f'{p["a"]} + {p["b"]} = {p["a"] + p["b"]}'
