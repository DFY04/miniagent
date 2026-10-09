from dataclasses import dataclass


from enum import Enum

class EventType(Enum):
    RUN_START  = 'run_start'      # 整轮任务开始
    LLM_START  = 'llm_start'      # 准备问模型
    LLM_END    = 'llm_end'        # 模型回话了
    TOOL_START = 'tool_start'     # 准备调工具
    TOOL_END   = 'tool_end'       # 工具返回了
    STEP_END   = 'step_end'       # 一步结束
    RUN_END    = 'run_end'        # 整轮结束


from typing import Callable, Dict, List


@dataclass
class Event:
    type: EventType
    data: dict          # 事件负载（如 {'name': 'sql_query', 'args': ...}）


class ControlFlowSignal(Exception):
    """控制流异常：意思是「流程要中断」。总线【不隔离】这类异常，直接往上抛。"""


class ApprovalRejected(ControlFlowSignal):
    """审批被拒绝（危险操作被拦下）。"""

    def __init__(self, tool_name: str = ''):
        super().__init__(f'人工拒绝执行工具 `{tool_name}`')
        self.tool_name = tool_name


class EventBus:
    def __init__(self):
        self._handlers = {}          # 事件类型 -> [处理函数]   （字典！subscribe 要 .setdefault）
        self.__all = []               # 通配订阅者             （列表！subscribe_all 要 .append）
    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:

        self._handlers.setdefault(event_type,[]).append(handler)
        #登记操作 setdefault(key,defaultvalue)是把字典类型的方法如果key的value不为空 直接返回key的值,否则返回defaultvalue,
        #此时_handlers为空,返回空列表[]在对列表.append

    def emit(self, event: EventType,**kwargs) -> None:

        for fn in self.__all:  # 先叫通配的
            try:
                fn(**kwargs)
            except ControlFlowSignal:
                raise                        # ★ 控制流异常：放行（审批拒绝要传出去）
            except Exception as e:
                print(f"{e}已隔离")
        for hanld in self._handlers.get(event,[]):
            try:
                hanld(**kwargs)
            except ControlFlowSignal:
                raise                        # ★ 同上
            except Exception as e:
                print(f"{e} 已经隔离")
    def subscribe_all(self, handler) -> None:
        self.__all.append(handler)




