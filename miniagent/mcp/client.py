"""MCP 客户端：连一个 MCP server（stdio），把它的工具变成 MiniAgent 的 BaseTool。

════════════════════════════════════════════════════════════════════
【为什么这里有个"桥"（_Bridge）？—— 这是整个接入唯一难的地方】

    你的代码                     MCP SDK
    ─────────────                ─────────────
    Agent.run()      同步          stdio_client()      async
    BaseTool.call()  同步          session.call_tool() async

    中间必须有人把 async 变成 sync。

    做法：**开一个后台线程，线程里跑一个 asyncio 事件循环**，
          调用时把协程丢进去、同步等结果。

    ★★ 但不能简单地"在主线程建连接、在后台线程用" ——
       stdio_client / ClientSession 内部是 anyio 的 task group，
       它们的进入和退出【必须在同一个 task 里】。
       所以这里让一个常驻协程（_serve）自己进出，别处只通过队列投递请求。

        ┌──────────── 后台线程（自己的 event loop）────────────┐
        │  _serve():                                          │
        │    async with stdio_client(...) as (r, w):           │
        │      async with ClientSession(r, w) as session:      │  ← 进出在同一个 task
        │        await session.initialize()                    │
        │        while True:                                   │
        │          req = await queue.get()   ← 主线程投递        │
        │          session.call_tool(...)    → fut.set_result() │
        └──────────────────────────────────────────────────────┘

    ★ 为什么用 concurrent.futures.Future 而不是 asyncio.Future？
      因为等结果的是【同步】的主线程：`fut.result(timeout=...)` 会阻塞主线程，
      而 asyncio.Future 只能在事件循环里 await。

════════════════════════════════════════════════════════════════════
【用法】

    with MCPClient.stdio('npx', ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
                         prefix='fs_') as mcp:
        agent = Agent(llm=llm, tools=mcp.tools)     # ★ 挂载，核心一行不用改
        list(agent.run([Message.user('列出 /tmp 下的文件')]))

    ★ 必须用 with：MCP server 是【子进程】，`__exit__` 里会关 session 并等它退出。
      不用 with 就会留下僵尸进程。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from miniagent.mcp.adapter import MCPTool

__all__ = ['MCPClient']


# ============================================================================
#  同步桥：后台线程 + 常驻协程
# ============================================================================

class _Bridge:
    """把 async 的 MCP session 包装成同步的 call_tool()。"""

    def __init__(self, params: StdioServerParameters, *,
                 connect_timeout: float = 30.0,
                 errlog: Optional[Any] = None):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name='mcp-bridge', daemon=True)
        self._thread.start()

        # ★ MCP server 的 stderr（日志/traceback）默认丢掉：
        #   它会跟我们的 stdout 混在一起，把输出冲成一团。调错时再开 verbose。
        self._errlog = errlog if errlog is not None else open(os.devnull, 'w', encoding='utf-8')
        self._own_errlog = errlog is None

        self._queue: Optional[asyncio.Queue] = None
        self._ready = threading.Event()
        self._init_error: Optional[BaseException] = None
        self._dead: Optional[BaseException] = None
        self._closed = False
        self._tools: List[Any] = []

        self._task = asyncio.run_coroutine_threadsafe(
            self._serve(params), self._loop)

        if not self._ready.wait(connect_timeout):
            self._force_stop()
            raise TimeoutError(f'MCP server {connect_timeout}s 内没有完成握手')
        if self._init_error is not None:
            self._force_stop()
            raise self._init_error

    # ------------------------------------------------------------------
    #  常驻协程：进程/session 的生死全归它管
    # ------------------------------------------------------------------
    async def _serve(self, params: StdioServerParameters) -> None:
        self._queue = asyncio.Queue()
        try:
            async with stdio_client(params, errlog=self._errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._tools = list((await session.list_tools()).tools)
                    self._ready.set()                      # ← 握手完成，构造函数可以返回了

                    while True:
                        req = await self._queue.get()
                        if req is None:                    # 关闭信号
                            break
                        name, args, fut = req
                        if fut.set_running_or_notify_cancel() is False:
                            continue
                        try:
                            result = await session.call_tool(name, args)
                            fut.set_result(result)
                        except BaseException as exc:       # noqa: BLE001
                            fut.set_exception(exc)
        except BaseException as exc:                       # noqa: BLE001
            if not self._ready.is_set():
                self._init_error = exc
            else:
                self._dead = exc
            self._ready.set()                              # 防止构造函数永久卡住
            raise
        finally:
            self._dead = self._dead or RuntimeError('MCP session 已结束')

    # ------------------------------------------------------------------
    #  同步侧
    # ------------------------------------------------------------------
    @property
    def tools(self) -> List[Any]:
        return self._tools

    def call_tool(self, name: str, args: Dict[str, Any],
                  *, timeout: float = 60.0) -> Any:
        """同步调用远程工具。阻塞主线程，等后台 loop 给结果。"""
        if self._closed:
            raise ConnectionError('MCP 客户端已关闭')
        if self._dead is not None:
            raise ConnectionError(f'MCP server 已断开：{self._dead}')

        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _enqueue() -> None:                            # 在 loop 线程里执行
            if self._dead is not None:
                fut.set_exception(ConnectionError(f'MCP server 已断开：{self._dead}'))
                return
            self._queue.put_nowait((name, args, fut))

        self._loop.call_soon_threadsafe(_enqueue)
        return fut.result(timeout=timeout)

    def close(self, *, timeout: float = 20.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._queue is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._task.result(timeout=timeout)             # 等 _serve 自己退出（含杀子进程）
        except BaseException:                              # noqa: BLE001
            pass                                           # 关的时候出错没什么好做的
        finally:
            self._force_stop()

    def _force_stop(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except BaseException:                              # noqa: BLE001
            pass
        self._thread.join(timeout=5)
        try:
            if not self._loop.is_closed():
                self._loop.close()
        except BaseException:                              # noqa: BLE001
            pass
        if getattr(self, '_own_errlog', False):
            try:
                self._errlog.close()
            except BaseException:                          # noqa: BLE001
                pass


# ============================================================================
#  对外的客户端
# ============================================================================

class MCPClient:
    """连一个 MCP server，暴露成一组 BaseTool。

    ★ 用 context manager 使用，退出时自动关 session + 杀子进程。
    """

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        prefix: str = '',
        connect_timeout: float = 30.0,
        verbose: bool = False,
    ):
        """
        Args:
            command: 可执行文件，如 'python' / 'npx' / 'uvx'
            args:    参数列表
            env:     子进程环境变量（默认继承当前进程，并强制 UTF-8）
            cwd:     子进程工作目录
            prefix:  工具名前缀（多个 server 都有同名工具时用，如 'fs_'）
            verbose: True = 保留 MCP server 的 stderr（看它自己的日志/报错）
        """
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.prefix = prefix
        self.connect_timeout = connect_timeout
        self.verbose = verbose

        # ★ 强制子进程用 UTF-8：Windows 默认 GBK，MCP 走 JSON/stdio，编码不对会直接炸。
        #   这跟 tools/sandbox.py 里设 PYTHONIOENCODING 是同一个道理。
        self.env = dict(os.environ) if env is None else dict(env)
        self.env.setdefault('PYTHONIOENCODING', 'utf-8')

        self._bridge: Optional[_Bridge] = None
        self._tools: List[MCPTool] = []

    @classmethod
    def stdio(cls, command: str, args: Sequence[str] = (), **kwargs: Any) -> 'MCPClient':
        """便捷构造（stdio 传输）。"""
        return cls(command, args, **kwargs)

    # ------------------------------------------------------------------
    def __enter__(self) -> 'MCPClient':
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
            encoding='utf-8',
            encoding_error_handler='replace',
        )
        self._bridge = _Bridge(params, connect_timeout=self.connect_timeout,
                               errlog=sys.stderr if self.verbose else None)
        self._tools = [MCPTool(spec, self._bridge, prefix=self.prefix)
                       for spec in self._bridge.tools]
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
        return False

    # ------------------------------------------------------------------
    @property
    def tools(self) -> List[MCPTool]:
        """挂到 Agent 上的工具列表。

        ★ 懒加载并【缓存】：这些 MCPTool 对象持有 bridge，
          必须在 with 块内使用（出了 with，bridge 已关闭）。
        """
        if self._bridge is None:
            raise RuntimeError('MCPClient 必须在 with 块内使用：服务器进程连着才有效')
        return self._tools

    def tool_names(self) -> List[str]:
        return [t.name for t in self.tools]

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None) -> str:
        """直接调一个远程工具（调试用，不走 Agent）。name 用【远程原名】。"""
        if self._bridge is None:
            raise RuntimeError('MCPClient 必须在 with 块内使用')
        from miniagent.mcp.adapter import result_to_text
        return result_to_text(self._bridge.call_tool(name, args or {}))

    def __repr__(self) -> str:
        state = 'open' if self._bridge is not None else 'closed'
        return f'MCPClient({self.command} {self.args!r}, {state}, tools={len(self._tools)})'
