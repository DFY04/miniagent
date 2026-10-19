"""MiniAgent 的 MCP 支持：把 MCP server 的工具挂成普通 BaseTool。

    MCPClient   连一个 MCP server（stdio），with 块内可用
    MCPTool     单个远程工具在本地的"替身"（BaseTool 子类）
    result_to_text / clean_schema / safe_function_name   纯函数，可单独测

★ 接入代价：只多了一个适配器类，core/agent.py 和 core/tool.py 一行没改。
  这就是"工具是接口，不是实现"的回报。

用法见 miniagent/mcp/demo.py。
"""
from miniagent.mcp.adapter import (
    MCPTool,
    clean_schema,
    result_to_text,
    safe_function_name,
)
from miniagent.mcp.client import MCPClient

__all__ = [
    'MCPClient',
    'MCPTool',
    'clean_schema',
    'result_to_text',
    'safe_function_name',
]
