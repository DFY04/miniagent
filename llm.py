"""LLM 适配器：复用 qwen-agent 的模型封装，只做「消息格式转换」。

设计原则：不重造 LLM HTTP 层。qwen-agent 已经处理好 DashScope / OpenAI 兼容
接口、流式、function calling 的方言差异，我们把它包一层即可。

转换方向：
    miniagent Message  <--(适配器)-->  qwen_agent Message
"""
from __future__ import annotations

from typing import List, Optional

from qwen_agent.llm import get_chat_model
from qwen_agent.llm.schema import FunctionCall as QFunctionCall
from qwen_agent.llm.schema import Message as QMessage

from miniagent.core.message import FunctionCall, Message


# ---------------- 格式转换 ----------------
def _to_qwen(m: Message):
    fc = None
    if m.function_call:
        fc = QFunctionCall(name=m.function_call.name, arguments=m.function_call.arguments)
    return QMessage(role=m.role, content=m.content or '', name=m.name, function_call=fc)


def _from_qwen(qm) -> Message:
    content = qm.content
    if isinstance(content, list):  # 多模态 content，M1 只取文本
        content = '\n'.join(getattr(x, 'text', None) or '' for x in content)

    fc = None
    if getattr(qm, 'function_call', None):
        fc = FunctionCall(name=qm.function_call.name, arguments=qm.function_call.arguments)

    return Message(role=qm.role, content=content or '', name=getattr(qm, 'name', None), function_call=fc)


class LLMClient:
    """对 Agent 暴露统一的 `chat(messages, tools) -> Message` 接口。"""

    def __init__(self, cfg: dict):
        self.model = get_chat_model(cfg)

    def chat(self, messages: List[Message], tools: Optional[List[dict]] = None) -> Message:
        qwen_messages = [_to_qwen(m) for m in messages]
        # qwen-agent 里全用真值判断（`if functions:`），所以空列表 [] 和 None 等价；
        # 这里显式转 None，只是让「没有工具」的意图更清楚。
        functions = tools or None

        last = None
        for chunk in self.model.chat(messages=qwen_messages, functions=functions, stream=True):
            last = chunk
        if not last:
            raise RuntimeError('LLM 没有返回任何内容')
        return _from_qwen(last[-1])
