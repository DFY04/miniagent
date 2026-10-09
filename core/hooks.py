from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List


class EventType(Enum):
    RUN_START  = 'run_start'      # 整轮任务开始
    LLM_START  = 'llm_start'      # 准备问模型
    LLM_END    = 'llm_end'        # 模型回话了
    TOOL_START = 'tool_start'     # 准备调工具
    TOOL_END   = 'tool_end'       # 工具返回了
    STEP_END   = 'step_end'       # 一步结束
    RUN_END    = 'run_end'        # 整轮结束


@dataclass
class Event:
    type: EventType
    data: dict = field(default_factory=dict)   # ★ 加了默认值，可以不传


class EventBus:
    def __init__(self):
        self._handlers: Dict[EventType, List[Callable]] = {}   # {事件类型: [处理函数, ...]}
        self._all: List[Callable] = []                          # ★ 通配订阅者（subscribe_all 用）

    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        # setdefault(key, default)：key 存在就返回它原来的值；不存在就先塞 [] 再返回。
        # 于是这一行 = "如果这个事件还没有列表就建一个，然后把 handler 追加进去"。
        self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Callable[[Event], None]) -> None:
        """订阅【所有】事件。用一个单独的列表存，不遍历 EventType。"""
        # ★ 原来的写法 `self._handlers.get(EventType)` 永远返回 None
        #   （_handlers 的键是 EventType.xxx 这些成员，不是 EventType 这个类本身）。
        self._all.append(handler)

    def emit(self, event_type: EventType, **kwargs) -> None:
        """广播。先叫通配订阅者，再叫专门订阅这个事件的。"""
        def _call(handler):
            try:
                handler(**kwargs)
            except Exception as e:                       # 异常隔离：一个崩了不影响别人
                print(f'  ⚠️ 订阅者 {getattr(handler, "__name__", handler)} 出错（已隔离）: {e}')

        for handler in self._all:                        # ★ 通配的先
            _call(handler)
        for handler in self._handlers.get(event_type, []):
            _call(handler)
