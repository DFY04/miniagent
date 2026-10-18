"""P14 · 多智能体编排。

    MultiAgentHub    —— 基类：成员管理 + 身份转换（把共享历史渲染成某个成员的视角）
    Orchestrator     —— 编排：每轮让裁判（一个 Agent）决定下一个该谁发言，上下文累积
"""
from miniagent.multiagent.hub import MultiAgentHub
from miniagent.multiagent.orchestrator import Orchestrator

__all__ = ['MultiAgentHub', 'Orchestrator']
