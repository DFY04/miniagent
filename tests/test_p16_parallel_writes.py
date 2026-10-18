"""test_p16_parallel_writes.py —— 并行 + 写文件（副作用）的验收测试（全离线）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p16_parallel_writes.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p16_parallel_writes

要解决的三个问题：
    [x] 两个 worker 同时写同一个文件 → 必须在派活【前】就被拦住
    [x] 动手前必须先把文件存进 Checkpoint（否则回滚毫无意义）
    [x] 事后必须知道"这一轮到底改了哪些文件"

关键设计点（都有测试盯着）：
    [+] 冲突检查发生在【执行之前】—— 冲突时 worker 一次都不该被调用
    [+] 路径要规范化：./a.txt 和 a.txt 是同一个文件，必须能撞上
    [+] 快照必须早于工具执行 —— 否则存的是"改后"版本，回滚没意义
    [+] 存档失败不打断任务，但错误必须记进 ckpt_error（不许静默）
    [+] 只读任务不产生任何快照（不碰文件就别存档）
    [+] 有冲突的任务不能被排进同一批（serialize 策略）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.checkpoint import Checkpoint  # noqa: E402
from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.multiagent.parallel import (  # noqa: E402
    FanOutOrchestrator,
    PlanError,
    SubTask,
    SyncExecutor,
    ThreadExecutor,
    find_write_conflicts,
    normalize_path,
    parse_plan,
)
from miniagent.tools.file_tools import WriteFileTool  # noqa: E402


# ============================================================================
# 测试替身
# ============================================================================

class FakeLLM:
    def __init__(self, reply: str = 'ok', sleep: float = 0.0):
        self.reply = reply
        self.sleep = sleep
        self.seen: List[List[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        if self.sleep:
            time.sleep(self.sleep)
        return Message.assistant(self.reply)


class WritingLLM:
    """第一次回答"我要调 write_file"，第二次回答"改完了"。"""

    def __init__(self, path: str, content: str, sleep: float = 0.0):
        self.path = path
        self.content = content
        self.sleep = sleep
        self.n = 0
        self.seen: List[List[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        self.n += 1
        if self.sleep:
            time.sleep(self.sleep)
        if self.n == 1:
            return Message.assistant(function_call=FunctionCall(
                name='write_file',
                arguments=json.dumps({'path': self.path, 'content': self.content},
                                     ensure_ascii=False),
            ))
        return Message.assistant(f'已写入 {self.path}')


class RecordingLLM(FakeLLM):
    """记录自己什么时候开始、什么时候结束（用来检查有没有重叠）。"""

    def __init__(self, name: str, log: List[Tuple[str, float, float]],
                 lock: threading.Lock, sleep: float = 0.0):
        super().__init__(name, sleep)
        self.name = name
        self.log = log
        self.lock = lock

    def chat(self, messages, tools=None):
        t0 = time.time()
        reply = super().chat(messages, tools)
        with self.lock:
            self.log.append((self.name, t0, time.time()))
        return reply


class FakeCheckpoint:
    """只记录 begin_step 被调成了几，不做真的文件操作。"""

    def __init__(self):
        self.root = Path.cwd()
        self.steps: List[int] = []
        self.snapshotted: List[str] = []

    def begin_step(self, step: int) -> None:
        self.steps.append(step)

    def snapshot_file(self, path):
        self.snapshotted.append(str(path))
        return Path(path).name


class BoomCheckpoint(FakeCheckpoint):
    def snapshot_file(self, path):
        raise OSError('磁盘满了')


# ============================================================================
# 小工具
# ============================================================================

def _plan(*items) -> str:
    """_plan((id, assignee, task, writes), ...) -> JSON 文本"""
    return json.dumps(
        [{'id': i, 'assignee': a, 'task': t, 'writes': list(w)}
         for i, a, t, w in items],
        ensure_ascii=False,
    )


def _orch(plan_text: str, agent_llms: dict, *, synth: bool = True, **kwargs):
    executor = kwargs.pop('executor', None) or SyncExecutor()
    agents = {n: Agent(llm=llm) for n, llm in agent_llms.items()}
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
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:                                  # noqa: BLE001
        raise AssertionError(f'期望 {exc_type.__name__}，实际 {type(exc).__name__}: {exc}') from exc
    raise AssertionError(f'期望抛 {exc_type.__name__}，但什么都没抛')


TASK = [Message.user('改几个文件')]


# ============================================================================
# 一、planner 输出里的 writes 字段
# ============================================================================

def test_parse_plan_accepts_writes():
    raw = _plan((1, '甲', '改 utils.py', ['utils.py']),
                (2, '乙', '查数据', []))
    plan = parse_plan(raw)
    assert plan[0]['writes'] == ['utils.py']
    assert plan[1]['writes'] == []


def test_parse_plan_defaults_to_empty_writes():
    """没写 writes → 视为只读任务（不报错，但也不会被保护）。"""
    raw = '[{"id": 1, "assignee": "甲", "task": "查一下"}]'
    assert parse_plan(raw)[0]['writes'] == []


def test_parse_plan_rejects_bad_writes():
    """writes 类型不对 → 报错，不能默默当成"没有写"。"""
    for bad in ['[{"assignee":"甲","task":"t","writes":"a.txt"}]',      # 不是列表
                '[{"assignee":"甲","task":"t","writes":[1]}]',          # 不是字符串
                '[{"assignee":"甲","task":"t","writes":[""]}]']:        # 空路径
        _raises(PlanError, lambda b=bad: parse_plan(b))


# ============================================================================
# 二、冲突检测（纯函数，不用起 Agent）
# ============================================================================

def test_path_normalization_makes_spellings_equal():
    """./a.txt、a.txt、sub/../a.txt 必须被认成同一个文件。"""
    base = Path.cwd()
    assert normalize_path('a.txt', base) == normalize_path('./a.txt', base)
    assert normalize_path('a.txt', base) == normalize_path('sub/../a.txt', base)


def test_find_conflicts_on_same_file():
    subs = [SubTask(id=1, assignee='甲', task='t', writes=['a.txt'], index=0),
            SubTask(id=2, assignee='乙', task='t', writes=['a.txt'], index=1)]
    conflicts = find_write_conflicts(subs, base=Path.cwd())
    assert len(conflicts) == 1
    first_id, second_id, path = conflicts[0]
    assert (first_id, second_id) == (1, 2)
    assert path.endswith('a.txt')


def test_find_conflicts_across_spellings():
    """不同写法但同一个文件，也要撞上 —— 这是规范化存在的唯一理由。"""
    subs = [SubTask(id=1, assignee='甲', task='t', writes=['a.txt'], index=0),
            SubTask(id=2, assignee='乙', task='t', writes=['./a.txt'], index=1)]
    assert find_write_conflicts(subs, base=Path.cwd())


def test_no_conflict_on_different_files():
    subs = [SubTask(id=1, assignee='甲', task='t', writes=['a.txt'], index=0),
            SubTask(id=2, assignee='乙', task='t', writes=['b.txt'], index=1)]
    assert find_write_conflicts(subs, base=Path.cwd()) == []


def test_readonly_tasks_never_conflict():
    subs = [SubTask(id=1, assignee='甲', task='t', writes=[], index=0),
            SubTask(id=2, assignee='乙', task='t', writes=[], index=1)]
    assert find_write_conflicts(subs, base=Path.cwd()) == []


# ============================================================================
# 三、reject 策略：派活【之前】就拦住
# ============================================================================

def test_reject_raises_before_any_worker_runs():
    """★★ 冲突必须发生在执行之前：worker 一次都不该被调用。

    为什么这条最重要？
        如果先跑起来再发现冲突，文件已经被写坏了 —— 那时再报错是"事后报警"，
        不是"防护"。
    """
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '也改 a.txt', ['a.txt']))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, planner_llm, synth_llm = _orch(plan_text, llms)

    try:
        list(orch.run(TASK))
    except PlanError as exc:
        assert '写冲突' in str(exc)
        assert 'a.txt' in str(exc)
    else:
        raise AssertionError('有写冲突居然没报错')

    assert planner_llm.seen, 'planner 应该被调用过（是它拆出来的冲突）'
    assert llms['甲'].seen == [] and llms['乙'].seen == [], \
        '★ 冲突的 worker 被调用了 —— 说明检查发生在执行之后，太晚了'
    assert synth_llm.seen == [], '压根没干活，不该进汇总'


def test_reject_allows_different_files():
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '改 b.txt', ['b.txt']))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, *_ = _orch(plan_text, llms)

    list(orch.run(TASK))
    assert orch.last_report.ok_count == 2
    assert orch.last_report.batches == 1


def test_bad_on_write_conflict_value():
    _raises(ValueError, lambda: FanOutOrchestrator(
        {'甲': Agent(llm=FakeLLM('a'))},
        planner=Agent(llm=FakeLLM('x')),
        on_write_conflict='isolate',      # 还没实现，必须拒绝
    ))


# ============================================================================
# 四、serialize 策略：冲突的分到不同批
# ============================================================================

def test_serialize_splits_conflicting_tasks_into_batches():
    """甲、乙抢 a.txt → 不能同批；丙写 b.txt → 可以跟甲同批。

    所以是 2 批： [甲, 丙] 然后 [乙]
    """
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '也改 a.txt', ['a.txt']),
                      (3, '丙', '改 b.txt', ['b.txt']))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b'), '丙': FakeLLM('c')}
    orch, *_ = _orch(plan_text, llms, on_write_conflict='serialize')

    list(orch.run(TASK))

    assert orch.last_report.batches == 2, f'应该分 2 批，实际 {orch.last_report.batches}'
    assert [r.index for r in orch.last_report.results] == [0, 1, 2], '结果顺序必须按计划顺序'
    assert orch.last_report.ok_count == 3


def test_serialize_keeps_conflicting_tasks_from_overlapping():
    """★ 真并行下验证：抢同一个文件的两个任务，时间上不能重叠。"""
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '也改 a.txt', ['a.txt']))
    log: List[Tuple[str, float, float]] = []
    lock = threading.Lock()
    llms = {
        '甲': RecordingLLM('甲', log, lock, sleep=0.06),
        '乙': RecordingLLM('乙', log, lock, sleep=0.06),
    }
    orch, *_ = _orch(plan_text, llms, on_write_conflict='serialize',
                     executor=ThreadExecutor(max_workers=2))

    list(orch.run(TASK))

    a = next(t for t in log if t[0] == '甲')
    b = next(t for t in log if t[0] == '乙')
    span_a = f'甲{a[1]:.3f}-{a[2]:.3f}'
    span_b = f'乙{b[1]:.3f}-{b[2]:.3f}'
    assert a[2] <= b[1] or b[2] <= a[1], \
        f'抢同一个文件的两个任务重叠了: {span_a} / {span_b}'


# ============================================================================
# 五、Checkpoint 接线：快照必须早于写入
# ============================================================================

def test_snapshot_happens_before_the_file_is_written(tmp_path):
    """★★ 端到端：真正写文件 + 真 Checkpoint。

    断言两件事：
        1. 文件最终是新内容（工具真的跑了）
        2. 影子目录里存的是【旧内容】—— 证明快照发生在写入之前
    """
    target = tmp_path / 'data.txt'
    target.write_text('old', encoding='utf-8')

    plan_text = _plan((1, '甲', '把 data.txt 改成 new', ['data.txt']))
    write_llm = WritingLLM('data.txt', 'new')
    agents = {'甲': Agent(llm=write_llm, tools=[WriteFileTool(cfg={'root': str(tmp_path)})])}
    ckpt = Checkpoint(str(tmp_path), [])

    orch = FanOutOrchestrator(
        agents,
        planner=Agent(llm=FakeLLM(plan_text)),
        executor=SyncExecutor(),
        checkpoint=ckpt,
        checkpoint_step=0,
    )
    list(orch.run(TASK))

    # 1. 工具真的写了
    assert target.read_text(encoding='utf-8') == 'new'

    # 2. 影子目录里是【旧】内容 → 快照早于写入
    shadow = tmp_path / '.agent' / 'checkpoints' / 'step-0000' / 'data.txt'
    assert shadow.is_file(), f'没找到快照: {shadow}'
    assert shadow.read_text(encoding='utf-8') == 'old', '存档里是"改后"的内容，回滚会失效'

    # 3. 事后知道改了哪个文件
    assert orch.last_report.results[0].written == ['data.txt']


def test_task_result_records_written_files(tmp_path):
    """TaskResult.written 必须记下真正存了档的文件（否则没法回滚也没法审计）。"""
    (tmp_path / 'a.txt').write_text('old-a', encoding='utf-8')
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']))
    agents = {'甲': Agent(llm=WritingLLM('a.txt', 'new-a'),
                          tools=[WriteFileTool(cfg={'root': str(tmp_path)})])}
    orch = FanOutOrchestrator(
        agents, planner=Agent(llm=FakeLLM(plan_text)),
        executor=SyncExecutor(), checkpoint=Checkpoint(str(tmp_path), []),
    )
    list(orch.run(TASK))

    r = orch.last_report.results[0]
    assert r.ok
    assert r.written == ['a.txt']
    assert r.ckpt_error == ''
    assert orch.last_report.to_dict()['tasks'][0]['written'] == ['a.txt']


def test_readonly_task_creates_no_snapshot(tmp_path):
    """只读任务（writes 为空）→ 一条快照都不该产生。"""
    plan_text = _plan((1, '甲', '查一下', []))
    ckpt = FakeCheckpoint()
    orch, *_ = _orch(plan_text, {'甲': FakeLLM('a')}, checkpoint=ckpt)

    list(orch.run(TASK))

    assert ckpt.snapshotted == [], f'只读任务居然存了档: {ckpt.snapshotted}'
    assert ckpt.steps == [], '没有 writes 就不该推进 Checkpoint 的时钟'


def test_checkpoint_step_uses_offset(tmp_path):
    """步号 = checkpoint_step + 子任务 index，这样每个子任务占一个 step。

    好处：回滚时可以精确回到"某一个子任务之前"。
    """
    (tmp_path / 'a.txt').write_text('a', encoding='utf-8')
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '也改 a.txt', ['a.txt']))
    ckpt = FakeCheckpoint()
    ckpt.root = tmp_path
    orch, *_ = _orch(plan_text, {'甲': FakeLLM('a'), '乙': FakeLLM('b')},
                     checkpoint=ckpt, checkpoint_step=10,
                     on_write_conflict='serialize')

    list(orch.run(TASK))

    assert ckpt.steps == [10, 11], f'步号应该是 [10, 11]，实际 {ckpt.steps}'


def test_snapshot_failure_does_not_break_the_task():
    """★ 存档失败不能打断任务（跟 mem_agent 的取舍一致），但错误必须记下来。

    少存一次档 = "这一步回不去"；抛出去 = 整个任务崩掉。
    但绝不能静默 —— TaskResult.ckpt_error 要有内容。
    """
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']))
    orch, *_ = _orch(plan_text, {'甲': FakeLLM('a')}, checkpoint=BoomCheckpoint())

    out = list(orch.run(TASK))
    r = orch.last_report.results[0]

    assert r.ok, '存档失败不该把任务判成失败'
    assert '磁盘满了' in r.ckpt_error, '存档失败必须留下痕迹'
    assert r.written == []
    assert out, '任务成功，应该有最终答案'


def test_no_checkpoint_means_no_snapshot_but_conflict_still_checked():
    """不传 checkpoint：不存档，但冲突检测照样生效（两者独立）。"""
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']),
                      (2, '乙', '也改 a.txt', ['a.txt']))
    llms = {'甲': FakeLLM('a'), '乙': FakeLLM('b')}
    orch, *_ = _orch(plan_text, llms)          # 没有 checkpoint

    _raises(PlanError, lambda: list(orch.run(TASK)))
    assert llms['甲'].seen == [], '冲突检查不该依赖 checkpoint'


def test_written_is_empty_without_checkpoint():
    plan_text = _plan((1, '甲', '改 a.txt', ['a.txt']))
    orch, *_ = _orch(plan_text, {'甲': FakeLLM('a')})

    list(orch.run(TASK))
    assert orch.last_report.results[0].written == []


# ============================================================================
# 脚本模式入口
# ============================================================================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        code = fn.__code__
        needs_tmp = 'tmp_path' in code.co_varnames[:code.co_argcount]
        try:
            if needs_tmp:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
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
