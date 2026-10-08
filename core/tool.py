"""MiniAgent 的工具系统：两级注册表。

设计（复刻 qwen-agent 并简化）：

  第一级  TOOL_REGISTRY : Dict[str, type]      —— 全局，名字 -> 工具「类」。由 @tool 装饰器在 import 时填充。
  第二级  Agent.tool_map: Dict[str, BaseTool]  —— 每个 Agent 实例一个，名字 -> 工具「实例」。

为什么要两级？
  - 类只需要定义一次、全局共享；
  - 实例在创建 Agent 时按需带配置（cfg），不同 Agent 的工具实例互不影响。

（对应 qwen-agent 的 tools/base.py 的 TOOL_REGISTRY + agent.py 的 self.function_map）
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type, Union

# ============ 第一级：全局工具目录（名字 -> 类） ============
# 值的类型是「BaseTool 的子类（类对象本身）」，所以用 Type[BaseTool] 而不是裸 type。
# （这里引用了下方才定义的 BaseTool，靠文件顶部的 `from __future__ import annotations` 兜底）
TOOL_REGISTRY: Dict[str, Type[BaseTool]] = {}


class ToolError(Exception):
    """工具执行失败时抛出，Agent 会把它转成 observation 回给模型。"""


def tool(name: str, allow_overwrite: bool = False):
    """装饰器：把一个 BaseTool 子类登记进全局 TOOL_REGISTRY。

    用法：
        @tool('my_add')
        class MyAdd(BaseTool):
            ...
    """

    def decorator(cls: Type[BaseTool]) -> Type[BaseTool]:
        if name in TOOL_REGISTRY and not allow_overwrite:
            raise ValueError(f'工具 `{name}` 已存在。如需覆盖请传 allow_overwrite=True。')
        cls.name = name
        TOOL_REGISTRY[name] = cls
        return cls

    return decorator


class BaseTool(ABC):
    """所有工具的基类。子类必须设置 name/description/parameters，并实现 call()。"""

    name: str = ''
    description: str = ''
    # 采用 OpenAI function calling 的 JSON schema（dict 形式，最标准）
    parameters: Dict[str, Any] = {'type': 'object', 'properties': {}, 'required': []}

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        if not self.name:
            raise ValueError(f'{type(self).__name__}.name 不能为空')

    @abstractmethod
    def call(self, params: Union[str, dict], **kwargs) -> str:
        """执行工具。params 是 LLM 传来的 JSON 字符串（或 dict）。返回字符串结果。"""

    @property
    def schema(self) -> dict:
        """工具说明书，会被喂给 LLM。"""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
        }

    @staticmethod
    def parse_params(params: Union[str, dict]) -> dict:
        """把 LLM 传来的参数（JSON 字符串）解析成 dict。"""
        if isinstance(params, dict):
            return params
        params = (params or '').strip()
        return json.loads(params) if params else {}
