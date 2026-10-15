"""demo.py —— 演示「上下文 + 外部文件」同步回滚。

跑法（在项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.checkpoint.demo

这个 demo 不联网、不调真模型，用「假 LLM + 假写文件工具」把整个流程走一遍，
重点看最后那一次 rollback：上下文和文件是不是【一起】回到了同一时刻。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from miniagent.checkpoint import Checkpoint
from miniagent.core.message import FunctionCall, Message


class FakeLLM:
    """按剧本返回 assistant 消息：每步都"想写一个文件"。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None):
        out = self.script[self.i]
        self.i += 1
        return out


# 剧本：三步，每步 model 想写一个文件（arguments 是 JSON 字符串）
SCRIPT = [
    Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'hello.txt', 'content': 'v2'}))),
    Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'hello.txt', 'content': 'v3'}))),
    Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': 'world.txt', 'content': 'hi'}))),
]


def write_file(root: Path, arguments: str) -> str:
    """假工具：把 arguments 里的 {path, content} 写到磁盘。"""
    args = json.loads(arguments)
    target = root / args['path']
    target.write_text(args['content'], encoding='utf-8')
    return 'ok'


def main():
    root = Path(tempfile.mkdtemp(prefix='checkpoint_demo_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')
    print(f'演示目录：{root}\n')

    # 上下文：和真实 agent 一样，system + user 在最前面（step = None）
    messages = [Message.system('你是一个会写文件的助手'),
                Message.user('把 hello.txt 改成 v3，再新建 world.txt')]

    ckpt = Checkpoint(root, messages)
    llm = FakeLLM(SCRIPT)

    # ---- 主循环：镜像 core/agent.py 的结构（方案 A）----
    start = ckpt.step                              # 新跑=0；回滚/重启后=恢复点
    for step in range(start, start + len(SCRIPT)):
        ckpt.begin_step(step)                      # ★ 唯一时钟：把循环变量照进来
        reply = llm.chat(messages)
        reply.step = step                          # ★ 上下文世界：盖【同一个】步号
        messages.append(reply)

        if not reply.function_call:
            break

        args = reply.function_call.arguments
        target = root / json.loads(args)['path']
        ckpt.snapshot_file(target)                 # ★ 文件世界：改之前存档（self.step == step）
        observation = write_file(root, args)       # ★ 真实改动磁盘
        ckpt.log_tool_call('write_file', args, observation)   # 只记账，不推进时钟
        messages.append(Message.function('write_file', observation, step=step))

    print('\n===== 跑完 3 步之后的状态 =====')
    print(f'  hello.txt = {(root / "hello.txt").read_text()!r}')
    print(f'  world.txt 存在 = {(root / "world.txt").exists()}')
    ckpt.dump_state()

    print('\n===== 回滚到 step 1 之前（撤销第 1、2 步）=====')
    ckpt.rollback(1)

    print('\n===== 回滚之后的状态 =====')
    print(f'  hello.txt = {(root / "hello.txt").read_text()!r}   （期望 v2）')
    print(f'  world.txt 存在 = {(root / "world.txt").exists()}    （期望 False）')
    ckpt.dump_state()

    # 断言：两个世界必须一致
    assert (root / 'hello.txt').read_text() == 'v2', '文件世界没回滚对！'
    assert not (root / 'world.txt').exists(), '新建的文件没删掉！'
    assert [m.step for m in messages] == [None, None, 0, 0], '上下文世界没回滚对！'
    print('\n[OK] 两个世界同步回滚成功：上下文和文件回到了同一时刻')


if __name__ == '__main__':
    main()
