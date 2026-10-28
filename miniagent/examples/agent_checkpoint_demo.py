"""P10 演示 —— 把 Checkpoint 接进 Agent 主循环（离线、不联网、不调真模型）。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.examples.agent_checkpoint_demo

【剧本】
    step 0 : 模型把 hello.txt 写成 'BAD'      ← 我们想反悔
    闸门问 : policy 第一次返回 0 → 回滚到 step 0 之前
    重跑 0 : 模型把 hello.txt 写成 'GOOD'     ← 撤销后重新来一次，这次对了
    step 1 : 模型说“全部改好了”，任务结束

【要看的四件事】
    ① 文件世界：hello.txt 从 BAD 变回 v1，再变成 GOOD（不是"BAD → GOOD"！）
                 —— 回滚把 BAD 这次写操作也一起撤销了
    ② 上下文世界：mem 里没有 'BAD' 那一轮的消息，模型下一轮"看不见"它
    ③ 步号：ckpt.step 从 0 重跑，两个世界用的是同一个数（不会错开一格）
    ④ 触发方式：policy 是注入的 —— 全程没碰 input()，跑完自动退出
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from miniagent.checkpoint import Checkpoint
from miniagent.core.message import FunctionCall, Message
from miniagent.memory.short_term import ShortTermMemory
from miniagent.runtime.rollback import RollbackGate
from miniagent.tools import file_tools                                  # noqa: F401  注册工具
from miniagent.core.mem_agent import Agent


class FakeLLM:
    """按剧本返回 assistant 消息（不联网）。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def chat(self, messages, tools=None):
        out = self.script[self.i]
        self.i += 1
        return out


def build_llm() -> FakeLLM:
    """三幕剧的剧本：BAD → GOOD → 收工。"""

    def write(path, content):
        return Message.assistant(function_call=FunctionCall(
            'write_file', json.dumps({'path': path, 'content': content})))

    return FakeLLM([
        write('hello.txt', 'BAD'),      # 第 1 次跑 step 0（会被回滚掉）
        write('hello.txt', 'GOOD'),     # 回滚后重跑 step 0
        Message.assistant('全部改好了'),  # step 1：没有工具调用 → 结束
    ])


class ScriptedGate:
    """一个只有两句话的"假闸门"：第一次说回滚，之后都说不回。

    ★ 它和 RollbackGate(policy=...) 是【同一个接口】：
      只要有 decide(step) -> Optional[int]，主循环就认（鸭子类型）。
      —— 这里故意不用 RollbackGate，是为了证明：
         "决策来源"和"回滚机制"这两件事是解耦的，谁都能当那个决策者。
    """

    def __init__(self, first: int):
        self.first = first
        self.asked = []

    def decide(self, step):
        self.asked.append(step)
        return self.first if len(self.asked) == 1 else None


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix='agent_ckpt_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')
    print(f'工作目录: {root}')
    print(f'初始: hello.txt = {(root / "hello.txt").read_text(encoding="utf-8")!r}\n')

    # ---- 装配：mem → ckpt（喂 mem.message 这个【引用】）→ agent ----
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)
    gate = ScriptedGate(first=0)          # 第一次问：回滚到 step 0 之前

    agent = Agent(
        llm=build_llm(),
        mem=mem,
        ckpt=ckpt,                        # ★ 接线 1：检查点
        rollback_gate=gate,               # ★ 接线 2：回滚决策
        tools=[{'name': 'write_file', 'root': str(root)}],
        max_steps=5,
    )

    print('===== 开始跑（step 0 结束后会回滚一次）=====')
    for msg in agent.run([Message.user('把 hello.txt 改成 GOOD')]):
        if msg.content:
            print(f'{msg.role}> {msg.content}')
    print()

    print('===== 结果 =====')
    print(f'hello.txt = {(root / "hello.txt").read_text(encoding="utf-8")!r}   ← 不是 BAD')
    print(f'闸门被问过几次: {gate.asked}   ← [0, 0]：回滚后 step 0 又跑了一遍')
    print(f'ckpt.step = {ckpt.step}')
    print(f'上下文里还有 BAD 吗: {any("BAD" in (m.content or "") for m in mem.message)}')
    print()
    ckpt.dump_state()

    # 顺手验证"回滚能力"还在：再手摇一次，回到 0 之前
    print('\n===== 手动再回滚一次（rollback(0)）=====')
    ckpt.rollback(0)
    print(f'hello.txt = {(root / "hello.txt").read_text(encoding="utf-8")!r}   ← 回到 v1')


if __name__ == '__main__':
    main()
