"""test_p10_file_tools.py —— 文件工具的 root 越界回归（全离线）。

【这条测试固化的是什么洞】
    tools/file_tools.py 旧实现：

        return (self.root / rel).resolve()

    pathlib 里「绝对路径做 `/` 的右操作数」会直接吞掉左边的 root：

        Path('E:/work') / 'C:/Windows/win.ini'  ==  Path('C:/Windows/win.ini')

    所以模型只要传一个绝对路径（或 `../x`），就能读写 root 之外的文件 ——
    Checkpoint 的 root 约束、沙箱的目录隔离全都被绕过去。

【修法】拼完 → resolve → 校验 `resolved.is_relative_to(root)`，越界抛 ValueError。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p10_file_tools.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.tools import file_tools  # noqa: E402,F401   import 一下，工具才注册
from miniagent.tools.file_tools import ReadFileTool, WriteFileTool  # noqa: E402


def _root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    return root


def _outside_secret(tmp_path):
    """root 之外的一个 secret 文件（模拟模型想偷的 .env）。"""
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_text('TOP-SECRET', encoding='utf-8')
    return secret


def _raises_value_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError('越界路径居然没报 ValueError')


# ==================== read_file 越界 ====================

def test_read_file_absolute_path_cannot_escape_root(tmp_path):
    """★ 回归：绝对路径不能读到 root 外的文件。"""
    root = _root(tmp_path)
    secret = _outside_secret(tmp_path)
    tool = ReadFileTool(cfg={'root': str(root)})

    msg = _raises_value_error(tool.call, {'path': str(secret)})
    assert '越界' in msg


def test_read_file_parent_traversal_cannot_escape_root(tmp_path):
    """`../secret.txt` 同样不能读到 root 外。"""
    root = _root(tmp_path)
    _outside_secret(tmp_path)
    tool = ReadFileTool(cfg={'root': str(root)})

    msg = _raises_value_error(tool.call, {'path': '../outside/secret.txt'})
    assert '越界' in msg


# ==================== write_file 越界 ====================

def test_write_file_absolute_path_cannot_escape_root(tmp_path):
    """★ 回归：绝对路径不能写到 root 外（这是 Checkpoint 回滚遮不住的洞）。"""
    root = _root(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'evil.txt'
    tool = WriteFileTool(cfg={'root': str(root)})

    msg = _raises_value_error(tool.call, {'path': str(target), 'content': 'pwned'})
    assert '越界' in msg
    assert not target.exists(), '越界写居然落地了'


def test_write_file_parent_traversal_cannot_escape_root(tmp_path):
    """`../evil.txt` 不能把文件写到 root 之外。"""
    root = _root(tmp_path)
    tool = WriteFileTool(cfg={'root': str(root)})

    msg = _raises_value_error(tool.call, {'path': '../evil.txt', 'content': 'pwned'})
    assert '越界' in msg
    assert not (tmp_path / 'evil.txt').exists()


# ==================== declare_writes 也要守住 ====================

def test_declare_writes_ignores_out_of_root_path(tmp_path):
    """declare_writes 越界时返回空列表（声明失败顶多少存一次档，不能打断主循环）。"""
    root = _root(tmp_path)
    secret = _outside_secret(tmp_path)
    tool = WriteFileTool(cfg={'root': str(root)})

    assert tool.declare_writes({'path': str(secret), 'content': 'x'}) == []
    inside = tool.declare_writes({'path': 'ok.txt', 'content': 'x'})
    assert len(inside) == 1 and inside[0].name == 'ok.txt'


# ==================== 正常路径不回归 ====================

def test_relative_paths_inside_root_still_work(tmp_path):
    """root 内的相对路径（含子目录）读写照旧。"""
    root = _root(tmp_path)
    writer = WriteFileTool(cfg={'root': str(root)})
    reader = ReadFileTool(cfg={'root': str(root)})

    writer.call({'path': 'sub/hello.txt', 'content': 'v1'})
    assert (root / 'sub' / 'hello.txt').read_text(encoding='utf-8') == 'v1'
    assert reader.call({'path': 'sub/hello.txt'}) == 'v1'


def test_missing_file_still_returns_error_string(tmp_path):
    """root 内不存在的文件：保持原来的“返回错误字符串、不抛异常”行为。"""
    root = _root(tmp_path)
    out = ReadFileTool(cfg={'root': str(root)}).call({'path': 'nope.txt'})
    assert out.startswith('错误')


# ==================== 脚本模式入口 ====================

def main() -> None:
    import tempfile
    from pathlib import Path

    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f'  ✅ {name}')
                passed += 1
            except Exception as e:
                print(f'  ❌ {name}  ->  {type(e).__name__}: {e}')
                failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
