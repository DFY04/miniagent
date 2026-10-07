"""MiniAgent 的消息结构。

刻意对齐主流 LLM 接口的四个要素：role / content / name / function_call，
并保持和 qwen-agent 的 schema 结构一致（这样适配器转换成本最低）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FunctionCall:
    """模型请求调用一个工具。arguments 是 JSON 字符串，如 '{"a": 1}'。"""

    name: str
    arguments: str

    def to_dict(self) -> Dict[str, str]:
        return {'name': self.name, 'arguments': self.arguments}


@dataclass
class Message:
    """一条消息。role ∈ {system, user, assistant, function}。"""

    role: str
    content: str = ''
    name: Optional[str] = None                    # 工具结果消息里 = 工具名
    function_call: Optional[FunctionCall] = None  # assistant 请求调工具时带上
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---- 便捷构造器（可读性优于手写 role=...）----
    @classmethod
    def system(cls, content: str) -> 'Message':
        return cls(role='system', content=content)

    @classmethod
    def user(cls, content: str) -> 'Message':
        return cls(role='user', content=content)

    @classmethod
    def assistant(cls,
                  content: str = '',
                  function_call: Optional[FunctionCall] = None,
                  name: Optional[str] = None) -> 'Message':
        return cls(role='assistant', content=content, function_call=function_call, name=name)

    @classmethod
    def function(cls, name: str, content: str) -> 'Message':
        return cls(role='function', name=name, content=content)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {'role': self.role, 'content': self.content}
        if self.name:
            d['name'] = self.name
        if self.function_call:
            d['function_call'] = self.function_call.to_dict()
        return d

    def __repr__(self) -> str:
        if self.function_call:
            return f'Message(role={self.role!r}, function_call={self.function_call.name}({self.function_call.arguments}))'
        return f'Message(role={self.role!r}, name={self.name!r}, content={self.content!r})'
