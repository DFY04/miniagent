"""P15 · FanOutOrchestrator 离线演示 —— 不需要 API key、不联网，直接跑。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.multiagent.parallel_demo

它演示三件事：
    1. 一个大任务被 planner 拆成 3 个【互不依赖】的子任务
    2. 三个 worker 的耗时条【真的重叠】了（不是嘴上海并行）
    3. 同一套编排逻辑，只换执行器（SyncExecutor ↔ ThreadExecutor）：
       加速比从 ≈1 变成 ≈3  —— 这就是"执行器可注入"的回报

看的时候盯住两件事：
    ★ 每个 worker 只收到【自己那一条子任务】，看不到别人（上下文隔离）
    ★ planner 和 synthesizer 各只被调用 1 次，worker 各 1 次（没有多余开销）
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Tuple

from miniagent.core.agent import Agent
from miniagent.core.message import Message
from miniagent.multiagent.parallel import (
    FanOutOrchestrator,
    ParallelReport,
    SyncExecutor,
    ThreadExecutor,
)

# ==================== 剧本 ====================

PLAN_TEXT = json.dumps({
    'tasks': [
        {'id': 'A', 'assignee': '调研员甲', 'task': '调研竞品 A 的价格策略'},
        {'id': 'B', 'assignee': '调研员乙', 'task': '调研竞品 B 的功能差异'},
        {'id': 'C', 'assignee': '调研员丙', 'task': '调研竞品 C 的用户口碑'},
    ]
}, ensure_ascii=False, indent=2)

REPLIES = {
    '调研员甲': 'A 家走低价路线，入门版 99 元/月，靠走量。',
    '调研员乙': 'B 家功能最全，但学习曲线陡，适合专业用户。',
    '调研员丙': 'C 家口碑两极：老用户夸稳定，新用户嫌界面旧。',
}

WORK_SECONDS = 0.15      # 每个 worker 假装要花 0.15 秒（模拟一次 LLM 调用）


# ==================== 假 LLM ====================

class PlannerLLM:
    """演"拆分者"：直接吐一份写死的 JSON 计划。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return Message.assistant(PLAN_TEXT)


class WorkerLLM:
    """演"干活的成员"：睡一会儿，然后回一句结论；顺便记下起止时间。"""

    def __init__(self, reply: str, timeline: List[Tuple[str, float, float]],
                 lock: threading.Lock):
        self.reply = reply
        self.timeline = timeline
        self.lock = lock
        self.seen: List[List[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        t0 = time.time()
        time.sleep(WORK_SECONDS)                      # 模拟等 LLM 回包
        with self.lock:
            self.timeline.append((self.reply[:6], t0, time.time() - t0))
        return Message.assistant(self.reply)


class SynthLLM:
    """演"汇总者"：把 N 份结果合成一句话。"""

    def __init__(self):
        self.calls = 0
        self.seen: List[List[Message]] = []

    def chat(self, messages, tools=None):
        self.calls += 1
        self.seen.append(list(messages))
        body = messages[-1].content or ''
        n = body.count('### 子任务')
        return Message.assistant(
            f'（汇总 {n} 份调研）结论：A 便宜、B 功能全、C 口碑两极。'
            f'建议先打价格战，再补功能。'
        )


# ==================== 打印工具 ====================

def hr(title: str = '', ch: str = '─', width: int = 74) -> None:
    if title:
        print(f'\n{ch * 3} {title} {ch * max(0, width - len(title) - 6)}')
    else:
        print(ch * width)


def gantt(timeline: List[Tuple[str, float, float]], scale: float = 0.025) -> None:
    """把 [(标签, 开始绝对时刻, 耗时)] 画成横条图 —— 一眼看出有没有重叠。"""
    if not timeline:
        print('  （无记录）')
        return
    base = min(t[1] for t in timeline)
    print(f'  时间轴（每格 ≈ {scale * 1000:.0f} ms）')
    for label, start, dur in sorted(timeline, key=lambda t: t[1]):
        offset = int(round((start - base) / scale))
        width = max(1, int(round(dur / scale)))
        print(f'  {label:<8} |{" " * offset}{"█" * width}')


def print_report(rep: ParallelReport) -> None:
    print(f'  {"子任务":<8}{"执行者":<10}{"状态":<8}{"耗时":>8}')
    for r in rep.results:
        state = 'ok' if r.ok else 'FAIL'
        print(f'  #{str(r.id):<7}{r.assignee:<10}{state:<8}{r.elapsed:>7.3f}s'
              + ('' if r.ok else f'   {r.error}'))
    print(f'  {"":<26}墙钟 {rep.wall_elapsed:.3f}s'
          f'   成功 {rep.ok_count}/{len(rep.results)}'
          f'   加速比 {rep.speedup}×')


# ==================== 跑一轮 ====================

def run_round(executor, title: str) -> Tuple[ParallelReport, List[Message], Dict[str, int]]:
    """用给定的执行器跑一轮，返回 (报告, 最终产出, 调用次数统计)。"""
    timeline: List[Tuple[str, float, float]] = []
    lock = threading.Lock()

    worker_llms: Dict[str, WorkerLLM] = {}
    agents: Dict[str, Agent] = {}
    for name, reply in REPLIES.items():
        llm = WorkerLLM(reply, timeline, lock)
        worker_llms[name] = llm
        agents[name] = Agent(llm=llm)

    planner_llm = PlannerLLM()
    synth_llm = SynthLLM()

    orch = FanOutOrchestrator(
        agents,
        planner=Agent(llm=planner_llm),
        synthesizer=Agent(llm=synth_llm),
        executor=executor,
    )

    print(f'\n【{title}】执行器 = {type(executor).__name__}')
    final = list(orch.run([Message.user('帮我做一份三家竞品的对比调研')]))
    rep = orch.last_report

    hr('时间轴', '-', 74)
    gantt(timeline)

    hr('报告', '-', 74)
    print_report(rep)

    hr('最终答案（汇总者产出）', '-', 74)
    for m in final:
        print(f'  [{m.agent_name}] {m.content}')

    return rep, final, {
        'planner': planner_llm.calls,
        'worker': sum(len(llm.seen) for llm in worker_llms.values()),
        'synth': synth_llm.calls,
    }


# ==================== 主流程 ====================

def main() -> None:
    print('=' * 74)
    print('P15 · FanOutOrchestrator 离线演示（fan-out / fan-in）')
    print('=' * 74)
    print(f'成员：{list(REPLIES)}')
    print(f'每个成员假装要干 {WORK_SECONDS * 1000:.0f} ms 的活')

    hr('① planner 拆出来的计划', '=', 74)
    print('  ' + PLAN_TEXT.replace('\n', '\n  '))

    # ---- 第 1 轮：串行执行器（对照组） ----
    rep_serial, _, calls = run_round(SyncExecutor(), '第 1 轮 · 串行')

    # ---- 第 2 轮：线程池执行器 ----
    rep_parallel, _, _ = run_round(ThreadExecutor(max_workers=3), '第 2 轮 · 并行')

    # ---- 对比 ----
    hr('对比（★ 同一套编排代码，只换了执行器）', '=', 74)
    print(f'  {"":<12}{"墙钟":>10}{"加速比":>10}')
    print(f'  {"SyncExecutor":<12}{rep_serial.wall_elapsed:>9.3f}s{rep_serial.speedup:>9}×')
    print(f'  {"ThreadExecutor":<12}{rep_parallel.wall_elapsed:>9.3f}s{rep_parallel.speedup:>9}×')
    print()
    print(f'  理论最优：3 个各 {WORK_SECONDS:.2f}s 的任务并行 → 墙钟 ≈ {WORK_SECONDS:.2f}s')
    print(f'  实际并行墙钟 {rep_parallel.wall_elapsed:.3f}s —— 时间条重叠了，才算真并行')

    hr('核对', '=', 74)
    print(f'  planner 被调用 {calls["planner"]} 次（拆分只做一次）')
    print(f'  worker 总共被调用 {calls["worker"]} 次（每个子任务一次）')
    print(f'  synthesizer 被调用 {calls["synth"]} 次（汇总只做一次）')
    print('  ★ 每个 worker 只收到自己那一条子任务，看不到别人 —— 上下文隔离')
    print('  ★ 所以身份转换（_render_view）在并行模式下根本用不上')


if __name__ == '__main__':
    main()
