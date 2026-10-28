"""MCP 工具 → MiniAgent 工具 的适配层（纯逻辑，不碰网络/进程）。

════════════════════════════════════════════════════════════════════
【这里只做三件"翻译"工作】

    ① 名字翻译   MCP 工具名 → 合法的 function name
                  （OpenAI/DashScope 只允许 ^[a-zA-Z0-9_-]{1,64}$）

    ② 参数说明书  MCP 的 input_schema → BaseTool.parameters
                  （MCP 的 schema 常带 $schema / title / additionalProperties，
                    部分厂商 API 会因此报错，所以要清洗成只留三样）

    ③ 结果翻译   CallToolResult（content 是块列表）→ 一个字符串
                  （BaseTool.call() 契约是"永远返回 str"）

★ 为什么把这三件单独放一个文件？
    它们都是【纯函数】，可以不启动 MCP server 就测。
    真正麻烦的进程/事件循环在 client.py 里，别混在一起。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from miniagent.core.tool import BaseTool

#: function name 的合法字符（和 OpenAI function calling 一致）
_BAD_NAME_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def safe_function_name(name: str, *, max_len: int = 64) -> str:
    """把任意名字净化成合法的 function name。

    '文件 读取' → '____' 这种情况会很丑，但总比 API 直接报错强。
    真要好看，请在挂载时给 prefix（如 'fs_'），让名字本来就合法。
    """
    cleaned = _BAD_NAME_CHARS.sub('_', str(name or '').strip())
    cleaned = cleaned[:max_len]
    return cleaned or 'mcp_tool'


def clean_schema(schema: Any) -> Dict[str, Any]:
    """MCP 的 input_schema → 厂商 API 能接受的 parameters。

    只保留三样：type / properties / required。
    其余（$schema、title、additionalProperties、definitions…）一律丢掉。

    ★ 为什么敢丢？
        这些字段是给"schema 校验器"看的，模型只需要知道"有哪些参数、什么类型、
        哪些必填"。带上多余的字段反而会被某些厂商 API 拒绝（400）。
    """
    if not isinstance(schema, dict):
        schema = {}

    props = schema.get('properties')
    if not isinstance(props, dict):
        props = {}

    required = schema.get('required')
    if not isinstance(required, list):
        required = []
    # 只保留确实存在于 properties 里的必填项（防止 schema 自身不一致）
    required = [r for r in required if r in props]

    out: Dict[str, Any] = {
        'type': schema.get('type') or 'object',
        'properties': props,
        'required': required,
    }
    return out


def result_to_text(result: Any) -> str:
    """CallToolResult → 字符串。

    MCP 的返回长这样：
        CallToolResult(content=[TextContent(type='text', text='...')], is_error=False)

    content 是个【块列表】，可能是：
        TextContent   -> 有 text
        ImageContent  -> 有 data / mime_type（我们只回报占位，不塞 base64 进上下文）
        EmbeddedResource / ResourceLink -> 只回报占位
    """
    parts: List[str] = []
    for block in (getattr(result, 'content', None) or []):
        btype = getattr(block, 'type', '') or 'unknown'
        if btype == 'text':
            parts.append(getattr(block, 'text', '') or '')
        elif btype == 'image':
            mime = getattr(block, 'mime_type', '') or 'image'
            parts.append(f'[图片：{mime}]')
        elif btype in ('resource', 'resource_link'):
            parts.append('[资源引用]')
        else:
            parts.append(f'[{btype}]')

    text = '\n'.join(p for p in parts if p).strip() or '(MCP 返回了空结果)'
    if getattr(result, 'is_error', False):
        text = '[MCP 错误] ' + text
    return text


# ============================================================================
#  适配器本体
# ============================================================================

class MCPTool(BaseTool):
    """一个远程 MCP 工具在本地的"替身"。

    它只做两件事：
        · 把 MCP 的 schema 挂到自己的 parameters 上（这样能混进 tool_schemas）
        · call() 时把参数转发给 server，把结果翻译成字符串

    ★ 它不管理连接 —— 连接（bridge）是外部传进来的。
      这样 MCPTool 可以脱离进程单独测（传个假 bridge 即可）。
    """

    def __init__(self, spec: Any, bridge: Any, *, prefix: str = ''):
        """
        Args:
            spec:   MCP 的 Tool 对象（有 name / description / input_schema）
            bridge: 有 call_tool(name, args) -> CallToolResult 的对象
            prefix: 挂载时的名字前缀（多个 server 防重名用，如 'fs_'）
        """
        self._remote_name = getattr(spec, 'name', '') or ''
        if not self._remote_name:
            raise ValueError('MCP 工具没有 name')

        # ★ BaseTool.__init__ 里会检查 name 非空，所以先设好再 super()
        self.name = safe_function_name(prefix + self._remote_name)
        self.description = (getattr(spec, 'description', None)
                            or f'MCP 工具 {self._remote_name}')

        # MCP 2.x 字段名是 input_schema；1.x 是 inputSchema —— 两个都兜一下
        schema = (getattr(spec, 'input_schema', None)
                  or getattr(spec, 'inputSchema', None))
        self.parameters = clean_schema(schema)

        self._bridge = bridge
        super().__init__(cfg=None)

    def call(self, params, **kwargs) -> str:
        """★ 遵守"永不抛异常"契约：任何失败都变成字符串回给模型。"""
        try:
            args = self.parse_params(params)
        except Exception as exc:                              # noqa: BLE001
            return f'MCP 工具 `{self.name}` 的参数不是合法 JSON：{exc}'

        try:
            result = self._bridge.call_tool(self._remote_name, args)
        except Exception as exc:                              # noqa: BLE001
            return f'MCP 工具 `{self.name}` 调用失败：{type(exc).__name__}: {exc}'

        return result_to_text(result)

    def __repr__(self) -> str:
        return f'MCPTool({self.name!r} ← remote {self._remote_name!r})'
