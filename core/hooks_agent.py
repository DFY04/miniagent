"""Agent 主循环（带事件埋点版）—— core/agent.py 的副本。

唯一区别：每走到关键点就 emit 一下。
**没动你原来的 agent.py**，你先看这份，觉得 OK 我再合进去。

埋点位置（7 处）：
    RUN_START   → run() 开头
    LLM_START   → 每次问模型之前
    LLM_END     → 模型回话之后
    TOOL_START  → 每次执行工具之前
    TOOL_END    → 工具返回之后
    STEP_END    → 一步结束
    RUN_END     → 任务结束（done / max_steps）
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Union

from miniagent.core.hooks import ApprovalRejected, EventType
from miniagent.core.message import FunctionCall, Message
from miniagent.core.tool import TOOL_REGISTRY, BaseTool


class Agent:
    """一个最小的工具调用 Agent（带 hooks）。

    llm 需要实现 `chat(messages, tools=None) -> Message`。
    bus 不传也能用 —— 那样就一个事件都不发。
    """

    def __init__(
        self,
        llm,
        system_message: str = '',
        tools: Optional[List[Union[str, dict, BaseTool]]] = None,
        max_steps: int = 10,
        bus=None,
    ):
        self.llm = llm
        self.system_message = system_message
        self.max_steps = max_steps
        self.bus = bus                          # ★ 新增
        self.tool_map: Dict[str, BaseTool] = {}
        for t in (tools or []):
            self.register_tool(t)

    # ---------------- 工具挂载（第二级注册表） ----------------
    def register_tool(self, t: Union[str, dict, BaseTool]) -> None:
        if isinstance(t, BaseTool):
            self.tool_map[t.name] = t
        elif isinstance(t, str):
            self.tool_map[t] = self._instantiate(t, cfg=None)
        elif isinstance(t, dict):
            name = t.get('name')
            if not name:
                raise ValueError(f'工具配置缺少 name 字段: {t}')
            self.tool_map[name] = self._instantiate(name, cfg=t)
        else:
            raise TypeError(f'不支持的工具类型: {type(t)}')

    @staticmethod
    def _instantiate(name: str, cfg: Optional[dict]) -> BaseTool:
        if name not in TOOL_REGISTRY:
            raise ValueError(f'工具 `{name}` 未注册。请确认已 import 对应模块。')
        return TOOL_REGISTRY[name](cfg)

    @property
    def tool_schemas(self) -> List[dict]:
        return [t.schema for t in self.tool_map.values()]

    # ---------------- 发事件的小助手 ----------------
    def _emit(self, event_type: EventType, **data) -> None:
        """★ 没传 bus 就什么都不做。
        有了它，run() 里就不用到处写 `if self.bus:`，可读性高很多。"""
        if self.bus:
            self.bus.emit(event_type, **data)

    # ---------------- 主循环 ----------------
    def run(self, messages: List[Message]) -> Iterator[Message]:
        """跑一次任务。逐个 yield 本轮新产生的消息。"""
        self._emit(EventType.RUN_START)                                    # ★ 1

        messages = list(messages)
        if self.system_message and (not messages or messages[0].role != 'system'):
            messages.insert(0, Message.system(self.system_message))

        for step in range(self.max_steps):
            self._emit(EventType.LLM_START, step=step)                     # ★ 2
            reply = self.llm.chat(messages, tools=self.tool_schemas)
            reply.step = step                 # ★ 盖上步号（在 LLM_END 之前，让 trace 里能看到）
            messages.append(reply)
            yield reply
            self._emit(EventType.LLM_END, reply=reply)                     # ★ 3

            if not reply.function_call:
                self._emit(EventType.RUN_END, reason='done')               # ★ 4
                return  # 没有工具调用 -> 任务结束

            tool_name = reply.function_call.name
            try:
                self._emit(EventType.TOOL_START, name=tool_name)            # ★ 5
            except ApprovalRejected as e:                                   # 审批订阅者拦下来了
                self._emit(EventType.RUN_END, reason='rejected')
                yield Message.assistant(f'[操作被拒绝] {e}')
                return
            observation = self._execute(reply.function_call)
            self._emit(EventType.TOOL_END, name=tool_name, result=observation)   # ★ 6

            fn_msg = Message.function(name=tool_name, content=observation, step=step)
            messages.append(fn_msg)
            yield fn_msg
            self._emit(EventType.STEP_END, step=step)                      # ★ 7

        # 步数用尽仍未结束
        self._emit(EventType.RUN_END, reason='max_steps')
        yield Message.assistant(f'[已达到最大步数 {self.max_steps}，任务中止]')

    def _execute(self, fc: FunctionCall) -> str:
        """执行一次工具调用，任何异常都转成字符串 observation（不中断 run）。"""
        if fc.name not in self.tool_map:
            return f'错误：没有名为 `{fc.name}` 的工具。可用工具：{list(self.tool_map)}'
        try:
            return str(self.tool_map[fc.name].call(fc.arguments))
        except Exception as e:  # noqa: BLE001
            return f'工具 `{fc.name}` 执行失败：{type(e).__name__}: {e}'
