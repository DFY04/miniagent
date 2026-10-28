"""MiniAgent —— 一个自研的 agent harness（面试项目）。

设计原则（M1）：
- 只做 qwen-agent 没做的"骨架"，LLM 调用直接复用 qwen-agent。不重造 LLM 层。
- 两级工具注册表：全局 TOOL_REGISTRY（名字->类）+ 每个 Agent 的 tool_map（名字->实例）。
- Agent 主循环：think -> act -> observe -> 终止（显式步数上限）。
"""

__version__ = '0.1.0'
