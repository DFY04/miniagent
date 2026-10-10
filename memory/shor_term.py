# short_term.py
from miniagent.core.message import Message


class ShortTermMemory:
    def __init__(self, max_tokens: int): ...
    def add(self, msg: Message) -> None: ...       # 加入并维护窗口
    def build(self) -> list[Message]: ...           # 返回压缩后的消息列表

