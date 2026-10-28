"""walkthrough.py —— 把「两个世界同步回滚」的每一步真实数据都摊开打印。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.checkpoint.walkthrough

这个文件不讲道理，只负责"把真相摆出来"：
每一步结束、以及回滚前后，都打印
    ① 上下文 messages 到底有哪些消息（含索引和 step）
    ② file_ops 表到底有哪些行
    ③ tool_calls 表到底有哪些行
    ④ 快照目录 .agent/checkpoints/ 下到底有哪些文件
    ⑤ 磁盘上 hello.txt / world.txt 的真实内容
你对照着看，就知道 step 是怎么把两个世界串起来的。

【方案 A 已落地】每进入一步先调 ckpt.begin_step(step)，
step 只有 agent 循环变量这一个来源，Checkpoint 不再自己数步数。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from miniagent.checkpoint import Checkpoint
from miniagent.core.message import FunctionCall, Message


def show_context(messages):
    print('  ① 上下文 messages：')
    for i, m in enumerate(messages):
        extra = ''
        if m.function_call:
            extra = f'  function_call={m.function_call.name}({m.function_call.arguments})'
        print(f'      [{i}] step={m.step!r:<5} role={m.role:<9} content={m.content!r}{extra}')


def show_db(ckpt):
    with ckpt._connect() as conn:
        print('  ② file_ops 表：')
        for row in conn.execute('SELECT step, op, rel_path FROM file_ops ORDER BY step'):
            print(f'      step={row[0]}  op={row[1]:<8} rel_path={row[2]}')
        print('  ③ tool_calls 表：')
        for row in conn.execute('SELECT step, tool, input FROM tool_calls ORDER BY step'):
            print(f'      step={row[0]}  tool={row[1]}  input={row[2]}')


def show_shadow(ckpt):
    print('  ④ 快照目录 .agent/checkpoints/：')
    found = False
    for d in sorted(ckpt.shadow_dir.glob('step-*')):
        for f in sorted(d.rglob('*')):
            if f.is_file():
                print(f'      {f.relative_to(ckpt.root)}  内容={f.read_text(encoding="utf-8")!r}')
                found = True
    if not found:
        print('      (空)')


def show_disk(root):
    print('  ⑤ 磁盘真实文件：')
    for name in ('hello.txt', 'world.txt'):
        p = root / name
        if p.exists():
            print(f'      {name} = {p.read_text(encoding="utf-8")!r}')
        else:
            print(f'      {name} 不存在')


def snapshot_state(title, ckpt, messages):
    print(f'\n{"=" * 70}\n{title}\n{"=" * 70}')
    show_context(messages)
    show_db(ckpt)
    show_shadow(ckpt)
    show_disk(ckpt.root)
    print(f'  self.step = {ckpt.step}')


def main():
    root = Path(tempfile.mkdtemp(prefix='walkthrough_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')

    messages = [Message.system('你是助手'), Message.user('改 hello + 建 world')]
    ckpt = Checkpoint(root, messages)

    print(f'root = {root}')
    snapshot_state('【起点】三个世界都还没动', ckpt, messages)

    # ---------- step 0 ----------
    print('\n>>> 进入 step 0：模型想写 hello.txt = v2')
    ckpt.begin_step(0)                              # ★ 唯一时钟：把循环变量照进来
    reply = Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'hello.txt', 'content': 'v2'})))
    reply.step = 0
    messages.append(reply)
    ckpt.snapshot_file(root / 'hello.txt')          # 改之前存档
    (root / 'hello.txt').write_text('v2', encoding='utf-8')
    ckpt.log_tool_call('write_file', '{"path":"hello.txt","content":"v2"}', 'ok')
    messages.append(Message.function('write_file', 'ok', step=0))
    snapshot_state('【step 0 结束】', ckpt, messages)

    # ---------- step 1 ----------
    print('\n>>> 进入 step 1：模型想写 hello.txt = v3')
    ckpt.begin_step(1)                              # ★ 唯一时钟：把循环变量照进来
    reply = Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'hello.txt', 'content': 'v3'})))
    reply.step = 1
    messages.append(reply)
    ckpt.snapshot_file(root / 'hello.txt')
    (root / 'hello.txt').write_text('v3', encoding='utf-8')
    ckpt.log_tool_call('write_file', '{"path":"hello.txt","content":"v3"}', 'ok')
    messages.append(Message.function('write_file', 'ok', step=1))
    snapshot_state('【step 1 结束】', ckpt, messages)

    # ---------- step 2 ----------
    print('\n>>> 进入 step 2：模型想新建 world.txt')
    ckpt.begin_step(2)                              # ★ 唯一时钟：把循环变量照进来
    reply = Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'world.txt', 'content': 'hi'})))
    reply.step = 2
    messages.append(reply)
    ckpt.snapshot_file(root / 'world.txt')
    (root / 'world.txt').write_text('hi', encoding='utf-8')
    ckpt.log_tool_call('write_file', '{"path":"world.txt","content":"hi"}', 'ok')
    messages.append(Message.function('write_file', 'ok', step=2))
    snapshot_state('【step 2 结束】', ckpt, messages)

    # ---------- rollback ----------
    print('\n>>> 出错！调用 ckpt.rollback(1)：撤销 step 1 和 step 2')
    ckpt.rollback(1)
    snapshot_state('【rollback(1) 之后】', ckpt, messages)


if __name__ == '__main__':
    main()
