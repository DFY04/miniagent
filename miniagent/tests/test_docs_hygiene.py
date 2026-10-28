"""test_docs_hygiene.py —— 防止「备课痕迹」docstring 回到代码里。

【背景】审计在源码里发现第二人称的教学批注：

    core/mem_agent.py     「没动你原来的 agent.py，你先看这份，觉得 OK 我再合进去」
    checkpoint/checkpoint.py「你贴的那个 AgentCheckpoint 例子」

这类痕迹会直接暴露"代码是照着参考实现填的"，是面试官一 grep 就能看到的硬伤。
文档清理本身没有运行时行为，所以用一条静态检查把它钉住，防止重新长回来。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_docs_hygiene.py -v
"""
from __future__ import annotations

from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]

# 第二人称 / 备课痕迹关键词（只在源码 docstring 里出现才算问题）
PREP_TRACES = ('你先看', '没动你原来的', '觉得 OK 我再合进去', '你贴的')


def _read(rel_path: str) -> str:
    return (PKG_ROOT / rel_path).read_text(encoding='utf-8')


def _assert_clean(rel_path: str) -> None:
    text = _read(rel_path)
    for trace in PREP_TRACES:
        assert trace not in text, f'{rel_path} 里又出现了备课痕迹：{trace!r}'


def test_mem_agent_docstring_has_no_prep_trace():
    """★ 回归：core/mem_agent.py 的 docstring 必须是正常技术文档语气。"""
    _assert_clean('core/mem_agent.py')


def test_checkpoint_docstring_has_no_prep_trace():
    """★ 回归：checkpoint/checkpoint.py 不能留“你贴的那个例子”这类批注。"""
    _assert_clean('checkpoint/checkpoint.py')


def test_hooks_agent_docstring_has_no_prep_trace():
    """同一段批注在 core/hooks_agent.py 里也复制了一份，一起钉住。"""
    _assert_clean('core/hooks_agent.py')


# ==================== 脚本模式入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  ✅ {name}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {name}  ->  {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
