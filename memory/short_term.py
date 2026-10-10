"""P04 —— 短期记忆（对话窗口）。

【这个文件存在的目的】
    给 Agent 一个"记得住、又不会撑爆上下文"的对话历史：
    消息一直往里塞，取出来的时候保证不超过 token 预算。

【和已有零件的关系】
    这个类**不实现压缩逻辑** —— 压缩在 core/context.py 的 truncate / compress 里。
    它只是"消息仓库 + 预算守卫"，到点了去调那两个函数。

【怎么用（未来接进 Agent 时）】
    mem = ShortTermMemory(max_tokens=8000, llm=llm)
    mem.add(Message.user('你好'))
    ...
    for msg in agent.run(mem.build()):      # build() 给的就是"安全的历史"
        ...
"""
from __future__ import annotations

from typing import List, Optional

from miniagent.core.context import compress, truncate
from miniagent.core.message import Message
from miniagent.core.protocols import ChatModel


class ShortTermMemory:
    """短期记忆：存对话；取的时候保证不超预算。

    【为什么要它】
        Agent 每一轮都要把历史喂给模型，但历史只会越来越长。
        这个类负责两件事：**记着所有消息**、**给出去时裁到预算内**。
    """

    def __init__(self, max_tokens: int, llm: Optional[ChatModel] = None):
        """【目的】记住"预算是多少"和"要不要用 LLM 做摘要"。

        Args:
            max_tokens: 这个记忆最多能占多少 token。
            llm: 用来做摘要的模型。
                 **不传的话，就退化成"只裁剪、不摘要"**（省钱，但会丢信息）。

        TODO: 实现 —— 存下这两个参数，并准备一个空的消息列表装历史。
        """
        raise NotImplementedError('TODO: __init__')

    def add(self, msg: Message) -> None:
        """【目的】把一条新消息记进来。

        注意：**只管"记"，不做压缩。**
        因为 add 会被频繁调用，每次都调 LLM 摘要太贵；
        压缩留到 `build()` 的时候统一做。

        TODO: 实现 —— 把 msg 追加到内部列表。
        """
        raise NotImplementedError('TODO: add')

    def build(self) -> list[Message]:
        """【目的】返回"可以喂给模型"的消息列表，保证不超过 max_tokens。

        规则：

            没超预算            → 原样返回
            超了 + 有 llm       → 用 compress（先摘要压老消息，再裁）
            超了 + 没有 llm     → 用 truncate（直接裁，丢信息但不崩）

        **调用者不用关心内部怎么压的，拿到手就是"安全的历史"。**

        TODO: 实现 —— 判断有没有 llm，分别调 compress 或 truncate。
        """
        raise NotImplementedError('TODO: build')
