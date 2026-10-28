"""MiniAgent 的消息结构。

刻意对齐主流 LLM 接口的四个要素：role / content / name / function_call，
并保持和 qwen-agent 的 schema 结构一致（这样适配器转换成本最低）。

【★ 关于 name 和 agent_name 的分工】

    上游（OpenAI / qwen-agent）只有一个 name 字段，而它的含义随 role 变：
        role='function'   -> name 是【工具名】
        role='assistant'  -> name 是【说话人】

    一个字段两种含义 = 每次用之前都得先看 role，多 agent 场景下极易写错。
    所以这里拆成两个语义互不重叠的字段：

        name        —— 只表示"哪个工具"（只对 role='function' 有意义）
        agent_name  —— 只表示"谁说的"（四种 role 全都有效）★

    有了 agent_name 之后：
        - 判断"这句是不是我说的" = `m.agent_name == me`，一眼就懂
        - 一个 Agent 调工具时产生的 function 消息也能盖上 agent_name，
          于是"自己的工具过程"和"别人的工具过程"可以分开处理
        - agent_name 是**纯内部的编排元数据**，不会发给 LLM API
          （见 miniagent/llm.py：适配器只透传 name，行为跟以前一模一样）

【关于 step 字段】
    trace_id 只能区分「这是哪一轮 run」，但一轮 run 里 Agent 会循环
    「想 → 做 → 观察 → 再想 → 再做 …」很多次。想区分「同一轮里的第几步」，
    光有 trace_id 不够，于是给 assistant / function 消息加了一个 step：

        trace_id  = 这一轮 run 的身份证（粗颗粒，一整轮不变）
        step      = 这一轮里的第几步（细颗粒，每问一次模型 +1）

    一个 step 完整地包含两条消息：
        assistant(function_call=...)  ← 模型第 step 次「想做某件事」
        function(name=..., ...)       ← 这件事的结果（observation）
    所以它们俩共享同一个 step 编号，这样回放时能一眼看出
    「第 2 步：模型想查表 → 拿到了表结构」。

    system / user 消息不在循环里产生，step 保持 None（默认值）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FunctionCall:
    """模型请求调用一个工具。arguments 是 JSON 字符串，如 '{"a": 1}'。"""

    name: str
    arguments: str

    def to_dict(self) -> Dict[str, str]:
        return {'name': self.name, 'arguments': self.arguments}


@dataclass
class Message:
    """一条消息。role ∈ {system, user, assistant, function}。"""

    role: str
    content: str = ''
    name: Optional[str] = None                    # 只对 role='function' 有意义 = 工具名
    agent_name: Optional[str] = None              # ★ 谁说的（多 agent 场景的身份）
    function_call: Optional[FunctionCall] = None  # assistant 请求调工具时带上
    step: Optional[int] = None                    # ★ 本轮 run 的第几步（system/user 是 None）
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---- 便捷构造器（可读性优于手写 role=...）----
    # ★ 注意参数顺序：content 永远在最前（保持老调用 Message.assistant('你好') 不破），
    #   step 放在最后且默认 None，所以不关心 step 的老代码完全不用改。
    @classmethod
    def system(cls, content: str) -> 'Message':
        return cls(role='system', content=content)

    @classmethod
    def user(cls, content: str, agent_name: Optional[str] = None) -> 'Message':
        return cls(role='user', content=content, agent_name=agent_name)

    @classmethod
    def assistant(cls,
                  content: str = '',
                  function_call: Optional[FunctionCall] = None,
                  name: Optional[str] = None,
                  agent_name: Optional[str] = None,
                  step: Optional[int] = None) -> 'Message':
        return cls(role='assistant', content=content,
                   function_call=function_call, name=name,
                   agent_name=agent_name, step=step)

    @classmethod
    def function(cls, name: str, content: str, step: Optional[int] = None) -> 'Message':
        # ★ 这里故意没有 agent_name 参数：function 消息的 agent_name 由编排层
        #   （Orchestrator）在写回共享历史前统一盖上 —— 因为"这是哪个 Agent 调的"
        #   是编排层才知道的信息，Agent 自己不知道自己在群里的名字。
        return cls(role='function', name=name, content=content, step=step)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {'role': self.role, 'content': self.content}
        if self.name:
            d['name'] = self.name
        if self.agent_name:
            d['agent_name'] = self.agent_name
        if self.function_call:
            d['function_call'] = self.function_call.to_dict()
        if self.step is not None:                 # ★ 只在有 step 时才写，避免 system/user 带上 null
            d['step'] = self.step
        return d

    def __repr__(self) -> str:
        if self.function_call:
            return f'Message(role={self.role!r}, step={self.step!r}, function_call={self.function_call.name}({self.function_call.arguments}))'
        return (f'Message(role={self.role!r}, agent_name={self.agent_name!r}, '
                f'name={self.name!r}, step={self.step!r}, content={self.content!r})')
