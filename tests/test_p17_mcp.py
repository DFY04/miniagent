"""test_p17_mcp.py —— MCP 接入验收（半离线：只起本地子进程，不联网）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p17_mcp.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p17_mcp

被测的东西：
    ① 三个纯函数（schema 清洗 / 名字净化 / 结果翻译）—— 不用起进程
    ② 真连一个本地 MCP server（miniagent/mcp/fake_server.py）——
       验证"list_tools → 挂到 Agent → Agent 调远程工具 → 结果回灌"整条链路

为什么要起真 server 而不是 mock？
    这条链路最容易错的地方是【协议/编码/进程生命周期】，
    mock 掉 session 就把这些全绕过去了，测了个寂寞。
    好在 fake_server 是本地进程、毫秒级启动，不算慢。
"""
from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.mcp import MCPClient, MCPTool  # noqa: E402
from miniagent.mcp.adapter import (  # noqa: E402
    clean_schema,
    result_to_text,
    safe_function_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ============================================================================
#  测试替身
# ============================================================================

class FakeBridge:
    """假装是 MCP 连接的桥：记录被调用的【远程名字】和参数。"""

    def __init__(self, result: Any = None, boom: BaseException | None = None):
        self.calls: List[tuple] = []
        self._result = result
        self._boom = boom

    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if self._boom is not None:
            raise self._boom
        return self._result


class FunctionCallLLM:
    """第一圈请求调工具，第二圈收工。顺便记录它收到的工具说明书。"""

    def __init__(self, name: str, arguments: str = '{}'):
        self.name = name
        self.arguments = arguments
        self.calls = 0
        self.seen_schemas: List[List[str]] = []

    def chat(self, messages, tools=None):
        self.seen_schemas.append([t['name'] for t in (tools or [])])
        self.calls += 1
        if self.calls == 1:
            return Message.assistant(function_call=FunctionCall(
                name=self.name, arguments=self.arguments))
        return Message.assistant('done')


@contextlib.contextmanager
def _mcp(**kwargs: Any):
    """起一个真的 fake MCP server（子进程）。"""
    kwargs.setdefault('connect_timeout', 30)
    with MCPClient.stdio(sys.executable,
                         ['-m', 'miniagent.mcp.fake_server'],
                         cwd=str(REPO_ROOT), **kwargs) as client:
        yield client


def _spec(name: str, schema: Any = None, desc: str = '') -> Any:
    """造一个假的 MCP Tool 对象（字段名对齐 mcp 2.x 的 input_schema）。"""
    return types.SimpleNamespace(name=name, description=desc,
                                 input_schema=schema if schema is not None else {})


def _result(blocks: List[Any], is_error: bool = False) -> Any:
    return types.SimpleNamespace(content=blocks, is_error=is_error)


def _text(t: str) -> Any:
    return types.SimpleNamespace(type='text', text=t)


# ============================================================================
#  一、纯函数：schema 清洗
# ============================================================================

def test_clean_schema_keeps_only_three_fields():
    """MCP 的 schema 带 $schema/title，厂商 API 会拒 —— 只留三样。"""
    raw = {
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'title': 'AddArgs',
        'type': 'object',
        'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}},
        'required': ['a', 'b'],
        'additionalProperties': False,
    }
    out = clean_schema(raw)
    assert out == {
        'type': 'object',
        'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}},
        'required': ['a', 'b'],
    }


def test_clean_schema_drops_required_not_in_properties():
    """schema 自己不一致（required 里写了不存在的字段）时，不能原样传给模型。"""
    out = clean_schema({'type': 'object',
                        'properties': {'a': {'type': 'string'}},
                        'required': ['a', 'ghost']})
    assert out['required'] == ['a']


def test_clean_schema_handles_garbage():
    for bad in (None, 'not-a-dict', 123, []):
        assert clean_schema(bad) == {'type': 'object', 'properties': {}, 'required': []}


# ============================================================================
#  二、纯函数：名字净化
# ============================================================================

def test_safe_function_name():
    """function name 只允许 [a-zA-Z0-9_-]，中文/空格/点号都要换掉。"""
    assert safe_function_name('read_file') == 'read_file'
    assert safe_function_name('文件 读取') == '_____'      # 5 个字符（含空格）→ 5 个下划线
    assert safe_function_name('a.b') == 'a_b'
    assert safe_function_name('') == 'mcp_tool'
    assert len(safe_function_name('x' * 200)) == 64


def test_prefix_is_applied_to_safe_name():
    """多个 server 都有同名工具时，靠 prefix 区分。"""
    tool = MCPTool(_spec('echo'), FakeBridge(), prefix='fs_')
    assert tool.name == 'fs_echo'
    # ★ 但真正发给 server 的必须是【原名】，不能带前缀
    tool.call('{"text": "x"}')
    assert tool._bridge.calls[0][0] == 'echo'


# ============================================================================
#  三、纯函数：结果翻译
# ============================================================================

def test_result_to_text_text_blocks():
    res = _result([_text('line1'), _text('line2')])
    assert result_to_text(res) == 'line1\nline2'


def test_result_to_text_marks_error():
    res = _result([_text('Error executing tool boom')], is_error=True)
    out = result_to_text(res)
    assert out.startswith('[MCP 错误]')
    assert 'boom' in out


def test_result_to_text_handles_empty_and_image():
    assert '空结果' in result_to_text(_result([]))
    img = types.SimpleNamespace(type='image', mime_type='image/png')
    assert '[图片：image/png]' in result_to_text(_result([img]))


# ============================================================================
#  四、真连 server：工具清单
# ============================================================================

def test_client_lists_tools():
    with _mcp() as mcp:
        names = mcp.tool_names()
        assert sorted(names) == ['add', 'boom', 'echo'], f'实际拿到 {names}'

        by_name = {t.name: t for t in mcp.tools}
        assert by_name['echo'].description, 'description 不能为空（模型靠它决定要不要调）'
        assert by_name['add'].parameters['required'] == ['a', 'b']
        assert set(by_name['add'].parameters['properties']) == {'a', 'b'}


def test_tools_are_mounted_on_agent():
    """★ 核心验收：MCP 工具能像本地工具一样挂到 Agent 上。"""
    with _mcp() as mcp:
        llm = FunctionCallLLM('echo', '{"text": "hi"}')
        agent = Agent(llm=llm, tools=mcp.tools)

        assert sorted(agent.tool_map) == ['add', 'boom', 'echo']
        # 挂载 == schemas 会被发给模型
        list(agent.run([Message.user('go')]))
        assert sorted(llm.seen_schemas[0]) == ['add', 'boom', 'echo']


# ============================================================================
#  五、真连 server：端到端调用
# ============================================================================

def test_agent_calls_remote_tool_end_to_end():
    """★ 完整链路：模型要调工具 → Agent 执行 MCPTool → 远程 server → 结果回灌。

    断言那条 function 消息里是 server 真正的返回值。
    """
    with _mcp() as mcp:
        agent = Agent(llm=FunctionCallLLM('add', '{"a": 37, "b": 58}'), tools=mcp.tools)
        produced = list(agent.run([Message.user('算一下')]))

    fn_msgs = [m for m in produced if m.role == 'function']
    assert fn_msgs, f'没有产生 function 消息：{[(m.role, m.content) for m in produced]}'
    assert fn_msgs[0].name == 'add', 'function 消息的 name 应该是工具名'
    assert float(fn_msgs[0].content) == 95.0, f'实际拿到 {fn_msgs[0].content!r}'


def test_remote_error_becomes_string_not_exception():
    """★ 远程工具抛异常 → 必须变成字符串，不能炸主循环。"""
    with _mcp() as mcp:
        agent = Agent(llm=FunctionCallLLM('boom'), tools=mcp.tools)
        produced = list(agent.run([Message.user('试一下')]))

    fn_msgs = [m for m in produced if m.role == 'function']
    assert fn_msgs
    assert '[MCP 错误]' in fn_msgs[0].content, fn_msgs[0].content
    assert produced[-1].content == 'done', '主循环应该继续并给出最终答案'


def test_direct_call_bypassing_agent():
    """不走 Agent 也能直接调（调试用）。"""
    with _mcp() as mcp:
        assert mcp.call_tool('echo', {'text': 'direct'}) == 'direct'


# ============================================================================
#  六、生命周期与错误
# ============================================================================

def test_client_is_closed_after_with():
    """出了 with 就不能再用 —— 否则会往已关闭的 bridge 里发请求。"""
    with _mcp() as mcp:
        assert mcp.tool_names()
    try:
        mcp.tools
    except RuntimeError as exc:
        assert 'with' in str(exc)
    else:
        raise AssertionError('出了 with 块还能拿 tools')


def test_bad_command_raises_quickly():
    """server 起不来 → 明确报错，不能静默卡死。"""
    try:
        with MCPClient.stdio('definitely-not-a-real-command-xyz', [],
                             connect_timeout=8):
            pass
    except Exception as exc:                                  # noqa: BLE001
        assert exc.__class__ is not None
    else:
        raise AssertionError('起不来的 server 居然没报错')


def test_tool_call_failure_is_string(monkeypatch=None):
    """桥本身就坏了（比如进程已死）→ 也必须变成字符串。"""
    tool = MCPTool(_spec('x'), FakeBridge(boom=ConnectionError('进程没了')))
    out = tool.call('{}')
    assert '调用失败' in out and '进程没了' in out


# ============================================================================
#  脚本模式入口
# ============================================================================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  [ok]   {name}')
            passed += 1
        except Exception as exc:                              # noqa: BLE001
            print(f'  [FAIL] {name}  ->  {type(exc).__name__}: {exc}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
