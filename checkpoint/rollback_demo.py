"""rollback_demo.py —— input() 回滚直接接入 agent 主循环的演示

跑法（项目根目录）：
    python -m miniagent.checkpoint.rollback_demo

不联网，用「假 LLM + 写文件工具」跑一个最小 agent 循环，每步结束问你是否回滚。

重点看四个接入点：
    1. ckpt.begin_step(step)       —— 唯一时钟（和 agent 循环变量对齐）
    2. tool 里 ckpt.snapshot_file() —— 改文件前存档（文件世界）
    3. 每步结束 input() 回滚确认     —— 主动回滚入口
    4. ckpt.rollback(target)        —— 上下文 + 文件一起回滚

【为什么用 while 而不是 for】
    回滚之后 ckpt.step 会被拨小，如果用 for step in range(...)，循环变量还在
    递增，下一轮 begin_step(大的数) 就把时钟又拨回去了，等于白回滚。
    所以这里用 while，回滚后手动 step = ckpt.step 对齐。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from miniagent.checkpoint import Checkpoint
from miniagent.core.message import FunctionCall, Message
from miniagent.memory.short_term import ShortTermMemory


# ============================================================
#  假零件：不联网、不调真模型
# ============================================================

class FakeLLM:
    """按剧本返回 assistant 消息。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None):
        out = self.script[self.i]
        self.i += 1
        return out


class WriteFileTool:
    """最小写文件工具：动手之前先让 ckpt 存档。"""

    name = 'write_file'

    def __init__(self, root: Path, ckpt: Checkpoint):
        self.root = root
        self.ckpt = ckpt

    def call(self, arguments: str) -> str:
        args = json.loads(arguments)
        path = self.root / args['path']
        self.ckpt.snapshot_file(path)          # ★ 文件世界：改之前存档
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args['content'], encoding='utf-8')
        return 'ok'


# ============================================================
#  ★ 核心：接入了回滚的 agent 主循环
#  （这就是把 input() 逻辑合进 core/mem_agent.py 的 run() 之后的样子）
# ============================================================

def run_with_rollback(llm, mem, ckpt, tools: dict, user_msg: str, max_steps: int = 5):
    """跑一轮 agent，每步结束问是否回滚。

    tools: {tool_name: tool_instance}，tool 需要有 .call(arguments_str) -> str
    """
    mem.add(Message.user(user_msg))

    step = ckpt.step                                 # 从恢复点开始（新跑=0）
    while step < max_steps:
        ckpt.begin_step(step)                        # ★ 1. 唯一时钟

        # ---- 想 ----
        history = mem.build()
        reply = llm.chat(history)
        reply.step = step                            # 上下文世界：同一个 step
        mem.add(reply)
        print(f'  [step {step}] assistant> {reply}')

        if not reply.function_call:
            break                                    # 没有工具调用 → 正常结束

        # ---- 做 ----
        tool_name = reply.function_call.name
        tool = tools.get(tool_name)
        if tool is None:
            observation = f'错误：没有工具 {tool_name}'
        else:
            observation = tool.call(reply.function_call.arguments)
        ckpt.log_tool_call(tool_name, reply.function_call.arguments, observation)  # 只记账

        fn_msg = Message.function(tool_name, observation, step=step)
        mem.add(fn_msg)
        print(f'  [step {step}] function> {observation}')

        # ---- ★ 3. 回滚确认（接入点）----
        if input('是否回滚?(y/n) ').strip().lower() == 'y':
            raw = input('回滚到第几步之前? ').strip()
            target = int(raw) if raw.isdigit() else ckpt.step   # 空/非法 = 撤销最近一步
            target = max(0, min(target, ckpt.step))             # 夹进合法范围
            ckpt.rollback(target)                               # ★ 4. 两世界一起回滚
            print(f'  → 已回滚到第 {target} 步之前，当前 step = {ckpt.step}')
            step = ckpt.step                                    # 时钟拨回，下一轮从这里继续
            continue

        step += 1                                    # 正常推进


# ============================================================
#  装配 + 跑
# ============================================================

def main():
    root = Path(tempfile.mkdtemp(prefix='rollback_demo_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')
    print(f'工作目录: {root}')
    print(f'初始: hello.txt = {repr((root / "hello.txt").read_text())}\n')

    # ---- 装配 ----
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)              # ★ 传 mem 内部列表的引用
    llm = FakeLLM([
        Message.assistant(function_call=FunctionCall(
            'write_file', json.dumps({'path': 'hello.txt', 'content': 'v2'}))),
        Message.assistant(function_call=FunctionCall(
            'write_file', json.dumps({'path': 'hello.txt', 'content': 'v3'}))),
        Message.assistant(function_call=FunctionCall(
            'write_file', json.dumps({'path': 'world.txt', 'content': 'hi'}))),
        Message.assistant('全部改好了'),
    ])
    tools = {'write_file': WriteFileTool(root, ckpt)}

    # ---- 跑 ----
    print('===== 开始对话（每步结束可选择回滚）=====')
    run_with_rollback(llm, mem, ckpt, tools, '把 hello.txt 改成 v3，再新建 world.txt')

    # ---- 最终状态 ----
    print('\n===== 最终状态 =====')
    print(f'hello.txt = {repr((root / "hello.txt").read_text()) if (root / "hello.txt").exists() else "不存在"}')
    print(f'world.txt 存在 = {(root / "world.txt").exists()}')
    print(f'ckpt.step = {ckpt.step}')
    print('上下文消息:')
    for m in mem.message:
        print(f'  step={m.step!r:<5} role={m.role:<9} {m.content!r}')


if __name__ == '__main__':
    main()
