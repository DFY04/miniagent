"""P02 —— 上下文截断。

TODO：把两个函数的函数体写出来（下面都是 NotImplementedError）。
写完跑  python -m miniagent.p02.test_p02  验证。
"""
from __future__ import annotations

from typing import List

from miniagent.core.message import Message


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本占多少 token（不要求精确，测试里会被替换掉）。

    建议规则：
      - 中文字符：1 字 ≈ 1 token
      - ASCII 字符：4 个字符 ≈ 1 token
      - 空字符串返回 0

    TODO: 实现它
    """
    raise NotImplementedError('TODO: estimate_tokens')


def truncate(messages: List[Message], max_tokens: int) -> List[Message]:
    """把消息列表裁到 token 预算内，返回**新的列表**（不要改原列表）。

    规则：
      1. system 消息永不裁
      2. 最后一条 user 消息永不裁
      3. 其余从【最老】开始删（= 尽量保留新的）
      4. 不能出现孤立的 function 消息（它前面那条 assistant 被删了，它也删）
      5. 保底：system + 最后一条 user 就算超预算也要留着

    TODO: 实现它
    """
    raise NotImplementedError('TODO: truncate')
