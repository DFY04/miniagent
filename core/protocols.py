"""MiniAgent 的「协议」（Protocol）—— 只描述"对象长什么样"，不强制继承。

为什么需要它？
    Python 是动态类型，`llm` 这种参数如果不写注解，类型检查工具（mypy / PyCharm）
    就把它当 `Any`，什么都查不出来。

    def summarize(messages: list[Message], llm) -> str:              # ❌ mypy 放弃检查
    def summarize(messages: list[Message], llm: ChatModel) -> str:   # ✅ 会检查

Protocol 的意思是"鸭子类型"：
    只要一个对象有下面这个 chat 方法，它就算符合 ChatModel，
    不需要继承任何东西 —— 真的 LLMClient 和测试里的 FakeLLM 都算。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from miniagent.core.message import Message


@runtime_checkable
class ChatModel(Protocol):
    """任何"能聊天"的对象。

    只要实现了 `chat(messages, tools=None) -> Message` 就算符合。
    `runtime_checkable` 让它还能用 isinstance(obj, ChatModel) 检查。
    """

    def chat(self,
             messages: list[Message],
             tools: Optional[list[dict]] = None) -> Message:
        ...


@runtime_checkable
class Subscriber(Protocol):
    """事件订阅者：任何"能被调用"的对象。

    （事件总线的 handler 就是这种：`def h(**kw) -> None`）
    """

    def __call__(self, **kwargs) -> None:
        ...
