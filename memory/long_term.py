"""P04 —— 长期记忆（跨会话检索）。

【这个文件存在的目的】
    短期记忆只能记住"这次对话"。
    长期记忆负责"从一堆文档里，把和当前问题相关的片段捞出来"。

    场景：用户问"我们去年营收多少？" —— 光靠对话历史答不出来，得去翻文档。

【关键设计：不重造轮子】
    向量化、切块、打分排序这些活，**qwen-agent 的 retrieval 工具已经做好了**。
    我们只做"包装"：把文档路径交给它、把问题交给它、把结果拿回来。

    对应 BLUEPRINT 里的原则：**复用不重造。**
"""
from __future__ import annotations

from typing import List, Optional


class LongTermMemory:
    """长期记忆：从外部文档里检索相关片段。

    【为什么要它】
        Agent 需要"外部知识"才能回答超出对话范围的问题。
        这个类就是"翻文档"的统一入口。

    【和 retrieval 工具的关系】
        LongTermMemory 自己不做检索，它是 retrieval 工具的**门面（Wrapper）**。
    """

    def __init__(self, retriever=None):
        """【目的】准备好"检索器"和一个空的文档清单。

        Args:
            retriever: 任何有 `.call({'query': ..., 'files': [...]})` 的对象。
                       **不传的话，就用 qwen-agent 的 retrieval 工具。**

                     为什么允许注入？—— 测试时可以塞个假的，
                     不用真的去解析 PDF、装 jieba/ppdfplumber 那些重依赖。
                     （和 summarize 收 llm 参数是一个道理：依赖注入）

        TODO: 实现 —— 存下 retriever（没传就用 qwen-agent 的 Retrieval），
                       并准备一个空的文档路径列表。
        """
        raise NotImplementedError('TODO: __init__')

    def add_documents(self, paths: list[str]) -> None:
        """【目的】告诉长期记忆"以后可以查这些文档"。

        Args:
            paths: 文件路径列表（本地路径、http 链接都行）。
                   支持的类型由 qwen-agent 的 retrieval 决定（txt/pdf/docx/html...）。

        多次调用应该**累积**（第二次调用不会把第一次的覆盖掉），
        而且重复的路径不该重复登记。

        TODO: 实现 —— 去重后追加到内部的文档列表。
        """
        raise NotImplementedError('TODO: add_documents')

    def retrieve(self, query: str) -> str:
        """【目的】从已登记的文档里，找出和 query 相关的片段，返回【文本】。

        返回值将来会被当作"知识"拼进 system 消息（接进 Agent 时用）。

        边界（很重要）：
            - **没登记任何文档** → 返回空字符串 `''`（不要报错）
            - **query 为空** → 返回空字符串

        因为"没有知识"是正常情况，不该让 Agent 崩掉。

        TODO: 实现 —— 把 query 和文档列表交给 retriever，返回它的结果。
        """
        raise NotImplementedError('TODO: retrieve')
