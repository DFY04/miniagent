"""P14 / P15 · 多智能体编排。

    MultiAgentHub        —— 基类：成员名册（谁在这个群里）+ 身份转换
    Orchestrator         —— 串行策略：每轮让裁判决定下一个该谁发言（handoff）
    FanOutOrchestrator   —— 并行策略：拆成互不依赖的子任务，同时跑，再汇总

★ 两个策略是【兄弟】：都继承 MultiAgentHub，但互相不 import。
  换策略 = 换一个类，已有的编排代码不用改。

    ┌───────────────────────────────────────────────────────────┐
    │ MultiAgentHub          成员名册                            │
    ├────────────────────────────┬──────────────────────────────┤
    │ Orchestrator（串行）        │ FanOutOrchestrator（并行）    │
    │ 共享上下文 → 需要身份转换   │ 上下文隔离 → 不需要           │
    │ 适用：辩论 / 评审 / 接力     │ 适用：批量处理 / 多路调研      │
    └────────────────────────────┴──────────────────────────────┘
"""
from miniagent.multiagent.hub import MultiAgentHub
from miniagent.multiagent.orchestrator import Orchestrator
from miniagent.multiagent.parallel import (
    FanOutOrchestrator,
    ParallelReport,
    PlanError,
    SubTask,
    SyncExecutor,
    TaskExecutor,
    TaskResult,
    ThreadExecutor,
    parse_plan,
)

__all__ = [
    # 基类
    'MultiAgentHub',
    # 串行策略
    'Orchestrator',
    # 并行策略
    'FanOutOrchestrator',
    'SubTask',
    'TaskResult',
    'ParallelReport',
    'PlanError',
    'parse_plan',
    # 可替换的执行器
    'TaskExecutor',
    'SyncExecutor',
    'ThreadExecutor',
]
