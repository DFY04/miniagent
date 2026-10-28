"""一个真的 MCP server（用官方 SDK 的 MCPServer 写的），给测试和 demo 用。

它不联网、不碰文件系统，只提供三个工具：

    echo(text)      原样返回文本
    add(a, b)       两数相加
    boom()          永远抛异常（测错误路径）

跑法（一般不用手动跑，客户端会把它当子进程拉起来）：
    python -m miniagent.mcp.fake_server
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer('miniagent-fake')


@server.tool()
def echo(text: str) -> str:
    """把传入的文本原样返回。"""
    return text


@server.tool()
def add(a: float, b: float) -> str:
    """计算两个数字的和。"""
    return str(a + b)


@server.tool()
def boom() -> str:
    """永远失败（用来验证错误会不会被包装成字符串）。"""
    raise RuntimeError('fake server 炸了')


if __name__ == '__main__':
    server.run('stdio')
