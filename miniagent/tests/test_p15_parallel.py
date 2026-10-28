"""test_p15_parallel.py —— P15 并行编排验收测试（全离线）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p15_parallel.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p15_parallel

DAILY_PLAN 的验收：
    [x] 能把大任务拆成多个子任务并发跑
    [x] 结果能汇总成最终答案
    [x] 部分子任务失败不影响整体

额外补的回归（都是"看起来对但实际错"的地方）：
    [+] 每个 worker 只能看到自己那条子任务（上下文隔离，否则并行毫无意义）
    [+] 结果必须按计划顺序归位（完成顺序是不确定的）
    [+] 并行要真的重叠（用墙钟时间证明，不是嘴上说说）
    [+] max_workers 要真的限制住同时在跑的数量
    [+] 同一个成员默认不并发（防数据竞争）—— 但可关闭
    [+] 控制流信号（预算超限/审批拒绝）不能被当成"子任务失败"吞掉
    [+] 串行策略和并行策略互相不 import（低耦合的硬性检查）
    [+] planner 乱输出时要报错，不能猜
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.hooks import ControlFlowSignal  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.multiagent.parallel import (  # noqa: E402
    FanOutOrchestrator,
    ParallelReport,
    PlanError,
    SyncExecutor,
    ThreadExecutor,
    parse_plan,
)


# ============================================================================
# 测试替身
# ============================================================================

class FakeLLM:
    """按剧本回答；可以假装"等了很久"（sleep）或"炸了"（boom）。"""

    def __init__(self, reply: str = 'ok', sleep: float = 0.0, boom: Optional[BaseException] = None):
        self.reply = reply
        self.sleep = sleep
        self.boom = boom
        self.seen: List[List[Message]] = []      # 每次被调用时看到的消息

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        if self.sleep:
            time.sleep(self.sleep)
        if self.boom is not None:
            raise self.boom
        return Message.assistant(self.reply)


class PeakTracker:
    """记录"同一时刻最多有几个在跑"。

    用法：`with tracker:` 包住要测量的那段，退出时统计峰值。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def __enter__(self):
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *exc_info):
        with self._lock:
            self.now -= 1

    def reset(self):
        with self._lock:
            self.now = 0
            self.peak = 0


class TrackedLLM(FakeLLM):
    """带峰值统计的 FakeLLM。"""

    def __init__(self, reply='ok', tracker=None, sleep=0.0, boom=None):
        super().__init__(reply, sleep, boom)
        self.tracker = tracker or PeakTracker()

    def chat(self, messages, tools=None):
        with self.tracker:
            return super().chat(messages, tools)


class StopNow(ControlFlowSignal):
    """模拟"预算超限 / 审批被拒"这类控制流信号。"""


# ============================================================================
# 小工具
# ============================================================================

def _plan_text(*pairs) -> str:
    """_plan_text(('甲','t1'), ('乙','t2')) -> JSON 计划文本。"""
    return json.dumps(
        [{'id': i, 'assignee': a, 'task': t} for i, (a, t) in enumerate(pairs, 1)],
        ensure_ascii=False,
    )


def _make(plan_text: str, worker_llms: dict, *, synth: bool = True, **kwargs):
    """建一个 FanOutOrchestrator。返回 (orch, planner_llm, synth_llm)。"""
    executor = kwargs.pop('executor', None) or SyncExecutor()
    agents = {name: Agent(llm=llm) for name, llm in worker_llms.items()}
    planner_llm = FakeLLM(plan_text)
    synth_llm = FakeLLM('FINAL') if synth else None
    orch = FanOutOrchestrator(
        agents,
        planner=Agent(llm=planner_llm),
        synthesizer=Agent(llm=synth_llm) if synth_llm else None,
        executor=executor,
        **kwargs,
    )
    return orch, planner_llm, synth_llm


def _raises(exc_type, fn):
    """断言 fn() 抛出 exc_type（不依赖 pytest，脚本模式也能跑）。"""
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:                                  # noqa: BLE001
        raise AssertionError(f'期望 {exc_type.__name__}，实际抛了 {type(exc).__name__}: {exc}') from exc
    raise AssertionError(f'期望抛 {exc_type.__name__}，但什么都没抛')


TASK = [Message.user('帮我做一份竞品调研')]


# ============================================================================
# 一、导出与低耦合
# ============================================================================

def test_exported_from_package():
    """两个策略都要能从包里直接导入（兄弟关系，平级对外）。"""
    from miniagent.multiagent import FanOutOrchestrator as F, Orchestrator as O
    assert F is FanOutOrchestrator
    assert O.__name__ == 'Orchestrator'


def test_strategies_do_not_import_each_other():
    """★ 低耦合的硬性检查：串行和并行的实现文件互相不 import。

    为什么值得写成测试？
        这种"架构约束"最容易在几个月后被某次改动破坏（有人图省事
        `from ...parallel import xxx`），而且破坏时不会有任何报错。
        用一条测试盯住它，比写在文档里靠谱。
    """
    import miniagent.multiagent.orchestrator as serial_mod
    import miniagent.multiagent.parallel as parallel_mod

    serial_src = Path(serial_mod.__file__).read_text(encoding='utf-8')
    parallel_src = Path(parallel_mod.__file__).read_text(encoding='utf-8')

    assert 'multiagent.parallel' not in serial_src, '串行策略不该 import 并行策略'
    assert 'multiagent.orchestrator' not in parallel_src, '并行策略不该 import 串行策略'


# ============================================================================
# 二、基本流程：拆分 → 并发 → 汇总
# ============================================================================

def test_planner_called_once_each_worker_once():
    """planner 只拆一次；每个子任务只派发一次（没有重复劳动）。"""
    plan = _plan_text(('甲', 't1'), ('乙', 't2'), ('丙', 't3'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b'), '丙': FakeLLM('c')}
    orch, planner_llm, synth_llm = _make(plan, llms)

    list(orch.run(TASK))

    assert len(planner_llm.seen) == 1, f'planner 被调了 {len(planner_llm.seen)} 次'
    assert {n: len(llm.seen) for n, llm in llms.items()} == {'甲': 1, '乙': 1, '丙': 1}
    assert len(synth_llm.seen) == 1


def test_results_ordered_by_plan_order():
    """★ 结果必须按计划顺序归位。

    真实并发下完成顺序是随机的（谁先跑完谁先到），
    所以不能用 append 顺序当结果顺序 —— 必须靠 id 归位。
    """
    plan = _plan_text(('甲', 't1'), ('乙', 't2'), ('丙', 't3'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b'), '丙': FakeLLM('c')}
    orch, *_ = _make(plan, llms)

    list(orch.run(TASK))

    assert [r.id for r in orch.last_report.results] == [1, 2, 3]
    assert [r.output for r in orch.last_report.results] == ['a', 'b', 'c']


def test_worker_only_sees_its_own_subtask():
    """★★ 上下文隔离：每个 worker 只拿到自己那一条，看不到别人的。

    这是并行模式和串行模式的根本区别：
        串行 → 共享时间线 → 需要 _render_view 做身份转换
        并行 → 上下文隔离 → 根本没这个问题
    """
    plan = _plan_text(('甲', '调研价格'), ('乙', '调研功能'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, *_ = _make(plan, llms)

    list(orch.run(TASK))

    for name, llm in llms.items():
        assert len(llm.seen) == 1
        msgs = llm.seen[0]
        assert len(msgs) == 1, f'{name} 看到了 {len(msgs)} 条消息，应该只有自己那条'
        assert msgs[0].role == 'user'

    assert llms['甲'].seen[0][0].content == '调研价格'
    assert llms['乙'].seen[0][0].content == '调研功能'
    # 甲看不到乙的任务
    assert '调研功能' not in llms['甲'].seen[0][0].content


def test_synthesizer_receives_all_results():
    """汇总者要能看到每一条子结果（含任务描述和产出）。"""
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    llms = {'甲': FakeLLM('甲的结果'), '乙': FakeLLM('乙的结果')}
    orch, _, synth_llm = _make(plan, llms)

    list(orch.run(TASK))

    body = synth_llm.seen[0][-1].content
    assert '甲的结果' in body and '乙的结果' in body
    assert 't1' in body and 't2' in body


def test_synthesizer_none_concatenates():
    """不传 synthesizer → 退化成"直接拼接"，每条都带来源名字。"""
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, _, synth_llm = _make(plan, llms, synth=False)

    out = list(orch.run(TASK))

    assert synth_llm is None
    assert len(out) == 2
    assert [m.agent_name for m in out] == ['甲', '乙']
    assert out[0].content == '[甲] a'


# ============================================================================
# 三、并行是真的吗？（用墙钟时间证明）
# ============================================================================

def test_parallel_actually_overlaps():
    """★★ 并行必须真的重叠。

    判据不用"看代码有没有写线程池"，而用【墙钟时间】：
        串行：3 个 80ms 的任务 → 墙钟 ≈ 240ms
        并行：3 个 80ms 的任务 → 墙钟 ≈ 80ms

    这里用"同一份计划跑两遍、比墙钟"的方式，而不是写死一个毫秒数 ——
    这样在快机器和慢机器上都成立，不会变成脆弱的 flaky 测试。
    """
    plan = _plan_text(('甲', 't1'), ('乙', 't2'), ('丙', 't3'))
    replies = {'甲': 'a', '乙': 'b', '丙': 'c'}
    sleep = 0.08

    def build(executor):
        llms = {n: FakeLLM(r, sleep=sleep) for n, r in replies.items()}
        return _make(plan, llms, executor=executor)[0]

    orch_sync = build(SyncExecutor())
    list(orch_sync.run(TASK))
    wall_sync = orch_sync.last_report.wall_elapsed

    orch_thread = build(ThreadExecutor(max_workers=3))
    list(orch_thread.run(TASK))
    rep = orch_thread.last_report

    assert wall_sync >= 3 * sleep * 0.9, f'串行墙钟 {wall_sync:.3f}s 太短，测试本身有问题'
    assert wall_sync > rep.wall_elapsed, f'并行没有比串行快：{wall_sync:.3f}s vs {rep.wall_elapsed:.3f}s'
    assert rep.wall_elapsed < wall_sync * 0.7, (
        f'并行墙钟 {rep.wall_elapsed:.3f}s，串行 {wall_sync:.3f}s —— 重叠得不够，可能没真并行'
    )
    assert rep.speedup > 1.7, f'加速比只有 {rep.speedup}×'


def test_sync_executor_matches_thread_executor_result():
    """换执行器不能改变结果 —— 只改变"快慢"。

    这就是把"并发机制"和"编排逻辑"解耦的回报：
    逻辑正确性可以用同步执行器确定性地测，不用跟线程的不确定性纠缠。
    """
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    replies = {'甲': 'a', '乙': 'b'}

    def run(executor):
        llms = {n: FakeLLM(r) for n, r in replies.items()}
        orch, *_ = _make(plan, llms, executor=executor)
        out = list(orch.run(TASK))
        return [m.content for m in out], orch.last_report.to_dict()['tasks']

    sync_out, sync_tasks = run(SyncExecutor())
    thread_out, thread_tasks = run(ThreadExecutor(max_workers=2))

    assert sync_out == thread_out
    assert [t['output'] for t in sync_tasks] == [t['output'] for t in thread_tasks]


def test_max_workers_limit():
    """★ 并发度必须被限制住（否则会把 API 打爆 / 触发 429）。"""
    names = ['甲', '乙', '丙', '丁']
    plan = _plan_text(*[(n, f't-{n}') for n in names])
    tracker = PeakTracker()
    llms = {n: TrackedLLM(n, tracker=tracker, sleep=0.05) for n in names}

    orch, *_ = _make(plan, llms, executor=ThreadExecutor(max_workers=2))
    list(orch.run(TASK))

    assert tracker.peak <= 2, f'同时在跑 {tracker.peak} 个，超过 max_workers=2'
    assert tracker.peak >= 2, f'峰值只有 {tracker.peak}，说明根本没并行起来（测试无效）'


def test_serialize_per_agent_default_no_concurrent_same_agent():
    """★ 默认：同一个成员不并发（"一个人一次只干一件"），防数据竞争。"""
    plan = _plan_text(('甲', 't1'), ('甲', 't2'))
    tracker = PeakTracker()
    llms = {'甲': TrackedLLM('a', tracker=tracker, sleep=0.05)}

    orch, *_ = _make(plan, llms, executor=ThreadExecutor(max_workers=2))
    list(orch.run(TASK))

    assert tracker.peak == 1, f'同一个成员被并发调用了（峰值 {tracker.peak}）'
    assert orch.last_report.wall_elapsed >= 0.09, '有锁的情况下，两个 50ms 任务应该串起来'


def test_serialize_per_agent_false_allows_concurrency():
    """关掉 per-agent 锁 → 同一个成员也能并发（前提：它的 llm 线程安全）。"""
    plan = _plan_text(('甲', 't1'), ('甲', 't2'))
    tracker = PeakTracker()
    llms = {'甲': TrackedLLM('a', tracker=tracker, sleep=0.05)}

    orch, *_ = _make(plan, llms, executor=ThreadExecutor(max_workers=2),
                     serialize_per_agent=False)
    list(orch.run(TASK))

    assert tracker.peak == 2, f'关了锁还是没并发（峰值 {tracker.peak}）'


# ============================================================================
# 四、失败处理
# ============================================================================

def test_partial_failure_collected():
    """★★ 部分失败：一个子任务挂了，其它照常，失败原因进报告。"""
    plan = _plan_text(('甲', 't1'), ('乙', 't2'), ('丙', 't3'))
    llms = {
        '甲': FakeLLM('a'),
        '乙': FakeLLM('b', boom=ValueError('工具挂了')),
        '丙': FakeLLM('c'),
    }
    orch, _, synth_llm = _make(plan, llms)

    out = list(orch.run(TASK))
    rep = orch.last_report

    assert rep.ok_count == 2 and rep.failed_count == 1
    failed = rep.failed_results[0]
    assert failed.id == 2 and not failed.ok
    assert 'ValueError' in failed.error and '工具挂了' in failed.error
    assert out, '有两条成功结果，应该还能给出最终答案'

    # ★ 失败信息也要传给汇总者，不能假装成功
    body = synth_llm.seen[0][-1].content
    assert '失败' in body and '工具挂了' in body


def test_on_error_raise():
    """on_error='raise' → 任何一个失败就整体抛错（快速失败）。"""
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b', boom=RuntimeError('坏了'))}
    orch, *_ = _make(plan, llms, on_error='raise')

    _raises(RuntimeError, lambda: list(orch.run(TASK)))


def test_control_flow_signal_propagates():
    """★ 控制流信号（预算超限 / 审批被拒）不能被当成"子任务失败"吞掉。

    它不是"这个子任务倒霉"，而是"整轮必须停"，所以要一路抛上去。
    """
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b', boom=StopNow('预算超限'))}
    orch, *_ = _make(plan, llms)

    _raises(StopNow, lambda: list(orch.run(TASK)))


# ============================================================================
# 五、planner 输出解析
# ============================================================================

def test_parse_plan_handles_markdown_fence():
    """LLM 很爱把 JSON 包在 ```json 围栏里。"""
    raw = '```json\n[{"id": 1, "assignee": "甲", "task": "t1"}]\n```'
    assert parse_plan(raw) == [
        {'id': 1, 'assignee': '甲', 'task': 't1', 'writes': []}
    ]


def test_parse_plan_handles_dict_wrapper():
    """也允许 {"tasks": [...]} 这种再包一层。"""
    raw = '{"tasks": [{"assignee": "甲", "task": "t1"}]}'
    assert parse_plan(raw) == [
        {'id': 1, 'assignee': '甲', 'task': 't1', 'writes': []}
    ]


def test_parse_plan_handles_explanatory_text():
    """前后有解释文字也能抠出来。"""
    raw = '好的，我拆成两个任务：\n[{"assignee": "甲", "task": "t1"},{"assignee": "乙", "task": "t2"}]\n希望有帮助！'
    plan = parse_plan(raw)
    assert [p['assignee'] for p in plan] == ['甲', '乙']


def test_parse_plan_rejects_garbage():
    """★ 乱输出要报错，不能猜。

    给默认值（比如"没人指派就交给第一个人"）等于替 planner 做决定：
    任务被派错人，但程序看起来成功了 —— 最难查的一类 bug。
    """
    for bad in ['', '   ', '这里没有 JSON', '[]',
                '[{"assignee": "甲"}]',            # 缺 task
                '[{"task": "t1"}]',                # 缺 assignee
                '[1, 2, 3]',                       # 不是对象
                '{"foo": 1}']:                     # 没有 tasks
        _raises(PlanError, lambda b=bad: parse_plan(b))


def test_unknown_assignee_raises_plan_error():
    """planner 把人名写错 → 当场报错，并列出合法成员。"""
    plan = _plan_text(('查无此人', 't1'))
    llms = {'甲': FakeLLM('a')}
    orch, *_ = _make(plan, llms)

    try:
        list(orch.run(TASK))
    except PlanError as exc:
        assert '查无此人' in str(exc)
        assert '甲' in str(exc), '报错里应该列出可用成员，方便定位'
    else:
        raise AssertionError('人名写错居然没报错')


def test_max_tasks_enforced():
    """拆分数量封顶：防 planner 一次拆出 1000 个任务把成本打爆。"""
    plan = _plan_text(('甲', 't1'), ('甲', 't2'), ('甲', 't3'))
    llms = {'甲': FakeLLM('a')}
    orch, *_ = _make(plan, llms, max_tasks=2)

    _raises(PlanError, lambda: list(orch.run(TASK)))


# ============================================================================
# 六、契约细节
# ============================================================================

def test_run_is_lazy():
    """★ 惰性：不迭代就什么都不发生（和 Agent.run / Orchestrator.run 一致）。"""
    plan = _plan_text(('甲', 't1'))
    llms = {'甲': FakeLLM('a')}
    orch, planner_llm, _ = _make(plan, llms)

    gen = orch.run(TASK)
    assert planner_llm.seen == [], '还没迭代就开始干活了'
    assert orch.last_report is None

    list(gen)
    assert len(planner_llm.seen) == 1
    assert orch.last_report is not None


def test_does_not_mutate_input_messages():
    """不能改调用方传进来的 messages 列表（否则第二次调用就脏了）。"""
    plan = _plan_text(('甲', 't1'))
    llms = {'甲': FakeLLM('a')}
    orch, *_ = _make(plan, llms)

    msgs = [Message.user('任务')]
    snapshot = list(msgs)
    list(orch.run(msgs))

    assert msgs == snapshot, '输入 messages 被改动了'


def test_empty_agents_rejected():
    """空名册没有意义 → 基类应该拦住。"""
    _raises(ValueError, lambda: FanOutOrchestrator(
        {}, planner=Agent(llm=FakeLLM('x'))))


def test_bad_on_error_value():
    """on_error 只认两个值，写错当场报错（别让拼写错误静默失效）。"""
    _raises(ValueError, lambda: FanOutOrchestrator(
        {'甲': Agent(llm=FakeLLM('a'))},
        planner=Agent(llm=FakeLLM('x')),
        on_error='ignore',
    ))


def test_report_metrics():
    """报告的派生指标：成功数 / 加速比 / to_dict。"""
    rep = ParallelReport(results=[], wall_elapsed=0.0)
    assert rep.ok_count == 0 and rep.failed_count == 0
    assert rep.speedup == 0.0, '墙钟为 0 时不能除零，要返回 0.0'
    d = rep.to_dict()
    assert d['wall_elapsed'] == 0.0 and d['tasks'] == []


def test_null_sleep_keeps_test_fast():
    """提醒：这些测试用假 LLM，总耗时应该远小于 2 秒（防止哪天真去调 API）。"""
    t0 = time.time()
    plan = _plan_text(('甲', 't1'), ('乙', 't2'))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, *_ = _make(plan, llms)
    list(orch.run(TASK))
    assert time.time() - t0 < 2.0


# ============================================================================
# 脚本模式入口
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
