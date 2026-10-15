"""test_checkpoint.py —— Checkpoint 验收测试（全离线、零网络）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/checkpoint/test_checkpoint.py -v
    .venv\\Scripts\\python.exe -m miniagent.checkpoint.test_checkpoint

验收标准（对应「同步回滚」这一目标）：
    [x] 上下文回滚：按 step 删掉 >= target 的消息，system/user（step=None）保留
    [x] 文件回滚：modified 的文件恢复到改之前，created 的文件被删掉
    [x] 同步：两个世界一起回到 target_step 之前，谁都不多不少
    [x] 边界（方案 A 语义）：合法目标是 0 <= target <= self.step；> self.step 报错
    [x] step 从 0 起（和 Message.step / 主循环对齐）
    [x] 方案 A：log_tool_call 不再自增，步号完全由 begin_step(step) 驱动
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.checkpoint import Checkpoint  # noqa: E402
from miniagent.core.message import Message  # noqa: E402


def _make(msgs=None):
    """建一个临时 root + Checkpoint，返回 (root, ckpt, messages)。"""
    root = Path(tempfile.mkdtemp(prefix='ckpt_test_'))
    messages = list(msgs) if msgs is not None else []
    ckpt = Checkpoint(root, messages)
    return root, ckpt, messages


# ==================== step 起点 ====================

def test_step_starts_at_zero():
    """★ step 从 0 起，和 Message.step / 主循环 for step in range(...) 对齐。"""
    _, ckpt, _ = _make()
    assert ckpt.step == 0


# ==================== 上下文回滚 ====================

def test_context_rollback_filters_by_step():
    """回滚到 step N：删掉所有 step >= N 的消息，system/user（None）保留。"""
    messages = [
        Message.system('sys'),
        Message.user('q'),
        Message.assistant('想写文件', step=0),
        Message.function('write_file', 'ok', step=0),
        Message.assistant('再写一次', step=1),
        Message.function('write_file', 'ok', step=1),
        Message.assistant('做完了', step=2),
    ]
    _, ckpt, messages = _make(messages)
    ckpt._rollback_context(1)

    assert [m.step for m in messages] == [None, None, 0, 0]
    assert [m.role for m in messages] == ['system', 'user', 'assistant', 'function']


# ==================== 文件回滚 ====================

def test_file_rollback_restores_modified():
    """modified：改之前快照，回滚后恢复到旧内容。"""
    root, ckpt, _ = _make()
    f = root / 'hello.txt'
    f.write_text('v1', encoding='utf-8')

    # step 0: v1 -> v2
    ckpt.begin_step(0)                       # ★ 方案 A：步号由循环变量显式驱动
    ckpt.snapshot_file(f)
    f.write_text('v2', encoding='utf-8')
    ckpt.log_tool_call('write_file', str(f), 'ok')

    # step 1: v2 -> v3
    ckpt.begin_step(1)
    ckpt.snapshot_file(f)
    f.write_text('v3', encoding='utf-8')
    ckpt.log_tool_call('write_file', str(f), 'ok')

    ckpt.rollback(1)
    assert f.read_text(encoding='utf-8') == 'v2'


def test_file_rollback_removes_created():
    """created：回滚后把新建的文件删掉。"""
    root, ckpt, _ = _make()

    # step 0：正常写一个
    f0 = root / 'a.txt'
    ckpt.begin_step(0)
    ckpt.snapshot_file(f0)
    f0.write_text('x', encoding='utf-8')
    ckpt.log_tool_call('write_file', str(f0), 'ok')

    # step 1：新建 b.txt
    f1 = root / 'b.txt'
    ckpt.begin_step(1)
    ckpt.snapshot_file(f1)
    f1.write_text('y', encoding='utf-8')
    ckpt.log_tool_call('write_file', str(f1), 'ok')

    ckpt.rollback(1)
    assert not f1.exists()
    assert f0.read_text(encoding='utf-8') == 'x'


# ==================== 同步 ====================

def test_sync_rollback_both_worlds():
    """★ 核心验收：上下文和文件一起回到 target_step 之前。"""
    root, ckpt, messages = _make([
        Message.system('sys'),
        Message.user('改 hello，再建 world'),
    ])
    hello = root / 'hello.txt'
    hello.write_text('v1', encoding='utf-8')

    # 模拟主循环 3 步（镜像 core/agent.py）
    for step, (path, content) in enumerate([
        ('hello.txt', 'v2'),
        ('hello.txt', 'v3'),
        ('world.txt', 'hi'),
    ]):
        reply = Message.assistant('想写文件', step=step)
        messages.append(reply)

        target = root / path
        ckpt.begin_step(step)                    # ★ 方案 A：循环变量 → Checkpoint
        ckpt.snapshot_file(target)
        target.write_text(content, encoding='utf-8')
        ckpt.log_tool_call('write_file', path, 'ok')
        messages.append(Message.function('write_file', 'ok', step=step))

    ckpt.rollback(1)

    # 文件世界
    assert hello.read_text(encoding='utf-8') == 'v2'
    assert not (root / 'world.txt').exists()
    # 上下文世界
    assert [m.step for m in messages] == [None, None, 0, 0]
    # 时钟也拨回来了
    assert ckpt.step == 1


# ==================== 边界 ====================

def test_cannot_rollback_forward():
    """只能回滚到已走过的步；target > self.step（未来）要报错。

    方案 A 下 self.step = 0 表示"当前在第 0 步"，
    回滚到 1（还没走到）是未来的事，非法。
    """
    _, ckpt, _ = _make()
    ckpt.begin_step(0)        # self.step = 0
    try:
        ckpt.rollback(1)      # 1 > 0：回滚到未来
    except ValueError:
        return
    raise AssertionError('回滚到未来应该抛 ValueError')


def test_rollback_to_current_step_is_allowed():
    """方案 A 语义：target == self.step 合法（撤锒最近这一步）。"""
    root, ckpt, _ = _make()
    f = root / 'a.txt'
    ckpt.begin_step(0)
    ckpt.snapshot_file(f)
    f.write_text('x', encoding='utf-8')
    ckpt.log_tool_call('write_file', str(f), 'ok')
    # 方案 A：log_tool_call 不再自增，self.step 仍是 0
    assert ckpt.step == 0
    ckpt.rollback(0)          # == self.step，合法：撤锒第 0 步
    assert not f.exists()


# ==================== 脚本模式入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  [OK] {name}')
            passed += 1
        except Exception as e:
            print(f'  [X] {name}  ->  {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
