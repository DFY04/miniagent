"""Agent 主循环：think -> act -> observe -> 终止。

这是 harness 的心脏，对应 qwen-agent 的 Agent._run + FnCallAgent._run，
但做了两点改进：
  1. 显式 max_steps 上限（防死循环；qwen-agent 是 MAX_LLM_CALL_PER_RUN 常量）
  2. 工具执行异常被捕获并作为 observation 回给模型，不会让整个 run 崩掉
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Union

from miniagent.core.message import FunctionCall, Message
from miniagent.core.tool import TOOL_REGISTRY, BaseTool


class Agent:
    """一个最小的工具调用 Agent。

    llm 需要实现 `chat(messages, tools=None) -> Message`（见 miniagent/llm.py）。
    注入自定义对象即可离线测试（见 miniagent/tests/test_m1_offline.py）。
    """

    def __init__(
        self,
        llm,
        system_message: str = '',
        tools: Optional[List[Union[str, dict, BaseTool]]] = None,
        max_steps: int = 10,
    ):
        self.llm = llm
        self.system_message = system_message
        self.max_steps = max_steps
        # 第二级：名字 -> 工具实例
        self.tool_map: Dict[str, BaseTool] = {}
        for t in (tools or []):
            self.register_tool(t)

    # ---------------- 工具挂载（第二级注册表） ----------------
    def register_tool(self, t: Union[str, dict, BaseTool]) -> None:
        """把一个工具挂到当前 Agent 上。支持三种写法（对应 qwen-agent _init_tool 的三分支）。"""
        if isinstance(t, BaseTool):
            # 已实例化，直接用
            self.tool_map[t.name] = t
        elif isinstance(t, str):
            # 字符串：查全局目录 -> 取类 -> 无配置实例化
            self.tool_map[t] = self._instantiate(t, cfg=None)
        elif isinstance(t, dict):
            # 带配置的 dict：查全局目录 -> 取类 -> 带配置实例化
            name = t.get('name')
            if not name:
                raise ValueError(f'工具配置缺少 name 字段: {t}')
            self.tool_map[name] = self._instantiate(name, cfg=t)
        else:
            raise TypeError(f'不支持的工具类型: {type(t)}')

    @staticmethod
    def _instantiate(name: str, cfg: Optional[dict]) -> BaseTool:
        if name not in TOOL_REGISTRY:
            raise ValueError(f'工具 `{name}` 未注册。请确认已 import 对应模块（@tool 装饰器会自动登记）。')
        return TOOL_REGISTRY[name](cfg)

    @property
    def tool_schemas(self) -> List[dict]:
        return [t.schema for t in self.tool_map.values()]

    # ---------------- 主循环 ----------------
    def run(self, messages: List[Message]) -> Iterator[Message]:
        """跑一次任务。逐个 yield 本轮新产生的消息（assistant 回复 / function 结果），
        yield 到最后一个 assistant 消息即得到最终答案。"""
        messages = list(messages)
        if self.system_message and (not messages or messages[0].role != 'system'):
            messages.insert(0, Message.system(self.system_message))

        for _ in range(self.max_steps):
            reply = self.llm.chat(messages, tools=self.tool_schemas)
            messages.append(reply)
            yield reply

            if not reply.function_call:
                return  # 没有工具调用 -> 任务结束

            observation = self._execute(reply.function_call)
            fn_msg = Message.function(name=reply.function_call.name, content=observation)
            messages.append(fn_msg)
            yield fn_msg

        # 步数用尽仍未结束：明确报错，而不是静默返回
        yield Message.assistant(f'[已达到最大步数 {self.max_steps}，任务中止]')

    def _execute(self, fc: FunctionCall) -> str:
        """执行一次工具调用，任何异常都转成字符串 observation（不中断 run）。"""
        if fc.name not in self.tool_map:
            return f'错误：没有名为 `{fc.name}` 的工具。可用工具：{list(self.tool_map)}'
        try:
            return str(self.tool_map[fc.name].call(fc.arguments))
        except Exception as e:  # noqa: BLE001 - 故意兜住所有异常回给模型
            return f'工具 `{fc.name}` 执行失败：{type(e).__name__}: {e}'
