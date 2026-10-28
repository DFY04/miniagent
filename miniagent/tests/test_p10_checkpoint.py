"""test_p10_checkpoint.py —— P10 验收测试（全离线、不联网、不阻塞、不敲键盘）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p10_checkpoint.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p10_checkpoint

分两组：
    A. RollbackGate  —— 决策层：字符串怎么变成步号、什么时候夹范围、
                        "不回滚"为什么必须用 None 而不是 0
    B. 接进 Agent    —— 回滚之后"文件 + 上下文 + 步号"三样是否同时回到从前

★ 关键测试技巧（沿用 P07 那套）：把副作用打成假的。
    input()  → 换成"按剧本吐答案"的函数，测试才不用等人敲键盘
    ckpt     → 换成真的（用 tmp 目录），因为它本来就是纯本地 sqlite + 文件复制
"""
from __future__ import annotations

import builtins
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.checkpoint import Checkpoint  # noqa: E402
from miniagent.core.hooks import EventBus, EventType  # noqa: E402
from miniagent.core.mem_agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.core.tool import BaseTool  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402
from miniagent.runtime.rollback import RollbackGate, clamp_target, parse_target  # noqa: E402
from miniagent.tools import file_tools  # noqa: E402,F401   import 一下，'write_file' 才注册


# ============================================================
#  公共零件
# ============================================================

class ScriptedLLM:
    """按剧本返回 assistant 消息（假模型）。剧本用完了就一直说"没别的了"。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen_histories = []          # 每次被问时，模型"看到"的历史长度

    def chat(self, messages, tools=None):
        self.seen_histories.append(len(messages))
        if not self.script:
            return Message.assistant('没别的了')
        return self.script.pop(0)


class ScriptedGate:
    """假闸门：按剧本回答"要不要回滚、回到哪一步"。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def decide(self, step):
        self.asked.append(step)
        if not self.answers:
            return None
        return self.answers.pop(0)


class BadDeclareTool(BaseTool):
    """★ 故意在 declare_writes 里抛异常：验证"存档失败不打断主循环"。"""

    name = 'bad_declare'
    description = '测试用'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def declare_writes(self, params):
        raise RuntimeError('我要在 declaring 阶段炸掉')

    def call(self, params, **kwargs):
        return '我还是干完活了'


def _write_msg(path, content):
    return Message.assistant(function_call=FunctionCall(
        'write_file', json.dumps({'path': path, 'content': content})))


def _says(text):
    return Message.assistant(text)


def _workdir():
    root = Path(tempfile.mkdtemp(prefix='p10_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')
    return root


def _read(root):
    return (root / 'hello.txt').read_text(encoding='utf-8')


def _build(root, script, gate=None, bus=None, tools=None, max_steps=5, max_rollbacks=3):
    """装配一套：mem + ckpt + agent。返回 (agent, mem, ckpt)。"""
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)
    agent = Agent(
        llm=ScriptedLLM(script),
        mem=mem,
        ckpt=ckpt,
        rollback_gate=gate,
        tools=tools if tools is not None else [{'name': 'write_file', 'root': str(root)}],
        max_steps=max_steps,
        max_rollbacks=max_rollbacks,
        bus=bus,
    )
    return agent, mem, ckpt


def _collect(agent, question='把 hello.txt 改一改'):
    return list(agent.run([Message.user(question)]))


@contextlib.contextmanager
def _fake_input(answers):
    """把 input() 换成"按剧本吐答案"，测完恢复原样。"""
    seq = list(answers)

    def _in(prompt=''):
        if not seq:
            raise AssertionError('input() 被多问了一次：剧本里的答案不够')
        return seq.pop(0)

    real = builtins.input
    builtins.input = _in
    try:
        yield
    finally:
        builtins.input = real


# ============================================================
#  A. RollbackGate：决策层
# ============================================================

def test_parse_target_rejects_empty_and_junk():
    """空行 / 乱敲 → None（不是 0！），交给调用方决定默认动作。"""
    assert parse_target('', 3) is None
    assert parse_target('   ', 3) is None
    assert parse_target('abc', 3) is None
    assert parse_target('-1', 3) is None, '负数不是"数字字符"，isdigit 会挡住'


def test_parse_target_accepts_digits_with_spaces():
    assert parse_target('2', 5) == 2
    assert parse_target('  2 ', 5) == 2, '用户手敲的空格必须容忍'
    assert parse_target('0', 5) == 0


def test_clamp_keeps_target_inside_range():
    assert clamp_target(99, 3) == 3, '滚到还没走过的未来 → 夹成当前步'
    assert clamp_target(-3, 3) == 0
    assert clamp_target(2, 3) == 2, '区间内的不动它'


def test_gate_none_means_no_rollback():
    gate = RollbackGate(policy=lambda step: None)
    assert gate.decide(0) is None


def test_gate_zero_is_a_real_target_not_a_falsy_sentinel():
    """★★ 最容易写错的一条：0 是合法的回滚目标（全撤销）。

    如果实现里写成 `if not target: return None`，0 会被当成"假值"吞掉 ——
    回滚到开头的请求会静默失效（不报错，就是不回滚）。
    """
    gate = RollbackGate(policy=lambda step: 0)
    assert gate.decide(3) == 0


def test_gate_clamps_policy_output():
    gate = RollbackGate(policy=lambda step: 99)
    assert gate.decide(2) == 2


def test_gate_never_touches_input_when_policy_injected():
    """注入 policy 后绝不碰 input() —— 这就是"不阻塞"的字面含义。"""
    gate = RollbackGate(policy=lambda step: 0)
    with _fake_input([]):            # 剧本是空的：真去问就 AssertionError
        assert gate.decide(5) == 0


def test_interactive_gate_user_says_no():
    with _fake_input(['n']):
        assert RollbackGate().decide(4) is None


def test_interactive_gate_user_says_yes_with_step():
    with _fake_input(['y', '2']):
        assert RollbackGate().decide(4) == 2


def test_interactive_gate_yes_but_empty_step_means_undo_last():
    with _fake_input(['Y', '']):     # 大写 Y + 空行
        assert RollbackGate().decide(4) == 4, '默认"撤销最近这一步"'


# ============================================================
#  B. 接进 Agent：回滚是不是"两个世界一起回去"
# ============================================================

def test_rollback_restores_file_and_context():
    """核心验收：step 0 写了 BAD → 回滚 → 重跑 step 0 写 GOOD。

    要同时满足：
      ① 磁盘上最终是 GOOD（不是 BAD）
      ② 上下文里【没有】BAD 那一轮的消息（模型下一轮看不见它）
      ③ 回滚后重跑用的还是 step 0（两个世界同一个时钟）
    """
    root = _workdir()
    gate = ScriptedGate([0, None])           # 第一次问：回滚到 0；之后：不回
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'BAD'),
                                     _write_msg('hello.txt', 'GOOD'),
                                     _says('全部改好了')], gate=gate)

    _collect(agent)

    assert _read(root) == 'GOOD'
    assert gate.asked == [0, 0], '回滚后 step 0 应该被重跑（闸门又被问了一次）'
    assert not any('BAD' in (m.content or '') for m in mem.message)
    assert ckpt.step == 1, '重跑完 step 0，再走一步到 step 1'


def test_rollback_deletes_tool_call_rows():
    """账本里 BAD 那次调用必须被删掉 —— 否则"回滚"只是假账。"""
    root = _workdir()
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'BAD'),
                                     _write_msg('hello.txt', 'GOOD'),
                                     _says('好了')],
                              gate=ScriptedGate([0, None]))
    _collect(agent)

    with ckpt._connect() as conn:
        rows = conn.execute('SELECT step, input FROM tool_calls ORDER BY step').fetchall()
    assert len(rows) == 1, f'只该剩下 GOOD 那一次，实际 {rows}'
    assert 'GOOD' in rows[0][1]


def test_both_worlds_share_one_step_number():
    """上下文里的 step 和文件账本里的 step 必须指同一件事。"""
    root = _workdir()
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'v2'), _says('好了')])
    _collect(agent)

    ctx_steps = {m.step for m in mem.message if m.step is not None}
    with ckpt._connect() as conn:
        file_steps = {r[0] for r in conn.execute('SELECT step FROM file_ops')}
    assert file_steps == {0}, '只该有一步动了文件'
    assert file_steps <= ctx_steps, '账本里的步号必须能在上下文里找到同号的消息'
    # 那一步的两条消息（assistant 请求 + function 结果）都盖着 step 0
    assert [m.role for m in mem.message if m.step == 0] == ['assistant', 'function']


def test_rollback_budget_stops_the_loop():
    """★ 回滚会把 step 拨小 → 理论上可以永远转下去。

    P07 的死循环检测管的是"工具重复调用"，管不到这个，所以需要单独一条上限。
    """
    root = _workdir()
    bus = EventBus()
    events = []
    bus.subscribe_all(lambda **kw: events.append(kw))

    gate = ScriptedGate([0] * 10)            # 永远喊回滚
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'x')] * 10,
                              gate=gate, bus=bus, max_rollbacks=2)

    out = _collect(agent)

    end = [e for e in events if e['event'] is EventType.RUN_END]
    assert end and end[-1]['reason'] == 'rollback_budget'
    assert any('回滚次数已达上限' in (m.content or '') for m in out)


def test_rollback_event_is_emitted():
    root = _workdir()
    bus = EventBus()
    events = []
    bus.subscribe_all(lambda **kw: events.append(kw))
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'BAD'),
                                     _write_msg('hello.txt', 'GOOD'),
                                     _says('好了')],
                              gate=ScriptedGate([0, None]), bus=bus)
    _collect(agent)

    rb = [e for e in events if e['event'] is EventType.ROLLBACK]
    assert len(rb) == 1
    assert rb[0]['target'] == 0 and rb[0]['step'] == 0


def test_missing_ckpt_keeps_old_behaviour():
    """★ 不传 ckpt = 整套回滚能力彻底不参与，老行为一字不变。"""
    root = _workdir()
    mem = ShortTermMemory(max_tokens=8000)
    agent = Agent(llm=ScriptedLLM([_write_msg('hello.txt', 'v2'), _says('好了')]),
                  mem=mem, tools=[{'name': 'write_file', 'root': str(root)}],
                  max_steps=5)
    out = _collect(agent)

    assert _read(root) == 'v2'
    assert not hasattr(agent, 'ckpt') or agent.ckpt is None
    assert any('好了' in (m.content or '') for m in out)


def test_declare_writes_failure_does_not_break_the_run():
    """存档失败必须只是"少存一次档"，不能把任务打断。"""
    root = _workdir()
    bus = EventBus()
    events = []
    bus.subscribe_all(lambda **kw: events.append(kw))

    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)
    agent = Agent(llm=ScriptedLLM([Message.assistant(function_call=FunctionCall('bad_declare', '{}')),
                                   _says('照样收工')]),
                  mem=mem, ckpt=ckpt, tools=[BadDeclareTool()],
                  max_steps=5, bus=bus)

    out = _collect(agent)

    assert any('照样收工' in (m.content or '') for m in out), '主循环不该被打断'
    errs = [e for e in events if e['event'] is EventType.CKPT_ERROR]
    assert errs, '失败要发 CKPT_ERROR 事件（可观测，不是静默）'


def test_read_only_tool_declares_nothing():
    """只读工具（默认 declare_writes 为空）→ 一个存档都不该产生。"""
    root = _workdir()
    agent, mem, ckpt = _build(
        root,
        [Message.assistant(function_call=FunctionCall('read_file', '{"path": "hello.txt"}')),
         _says('读了')],
        tools=[{'name': 'read_file', 'root': str(root)}],
    )
    _collect(agent)

    assert not ckpt.shadow_dir.exists() or not list(ckpt.shadow_dir.glob('step-*'))
    with ckpt._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM file_ops').fetchone()[0] == 0


def test_snapshot_then_manual_rollback_to_zero():
    """手摇一次：跑到一半 rollback(0) → 文件回 v1、上下文只剩 user。"""
    root = _workdir()
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'v2'), _says('好了')])
    _collect(agent)
    assert _read(root) == 'v2'

    ckpt.rollback(0)

    assert _read(root) == 'v1'
    assert [m.role for m in mem.message] == ['user']
    assert ckpt.step == 0


def test_restart_resumes_from_ckpt_step():
    """★ 重启继续跑：同一个 ckpt 再跑一轮，应该从 ckpt.step 接着走而不是从 0 重来。

    这也是为什么主循环的起点写成 `step = ckpt.step`（而不是固定 0）。
    """
    root = _workdir()
    agent, mem, ckpt = _build(root, [_write_msg('hello.txt', 'v2'), _says('好了')])
    _collect(agent)
    assert ckpt.step == 1

    # 换新的 mem（新的一轮上下文），但 ckpt 复用 —— 进度应该接得上
    mem2 = ShortTermMemory(max_tokens=8000)
    agent2 = Agent(llm=ScriptedLLM([_write_msg('hello.txt', 'v3'), _says('又好了')]),
                   mem=mem2, ckpt=ckpt,
                   tools=[{'name': 'write_file', 'root': str(root)}], max_steps=5)
    _collect(agent2)

    steps = sorted({m.step for m in mem2.message if m.step is not None})
    assert steps == [1, 2], f'新的一轮该从 step 1 继续，实际 {steps}'
    assert _read(root) == 'v3'


# ============================================================
#  C. 一步多工具：tool_calls 用 (step, seq) 复合主键
# ============================================================

def test_two_tool_calls_in_same_step_do_not_crash():
    """★ 回归：一步调两次工具不能 IntegrityError（并行 tool_calls 的预演）。

    旧 schema 是 `step INTEGER PRIMARY KEY` + 纯 INSERT —— 同一步第二次调用直接
    `sqlite3.IntegrityError: UNIQUE constraint failed`。
    """
    root = _workdir()
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)

    ckpt.begin_step(0)
    ckpt.log_tool_call('read_file', '{"path": "a.txt"}', 'r1')
    ckpt.log_tool_call('read_file', '{"path": "b.txt"}', 'r2')   # 旧实现这里炸

    with ckpt._connect() as conn:
        rows = conn.execute(
            'SELECT step, seq, input FROM tool_calls ORDER BY step, seq'
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, 0), (0, 1)]
    assert [r[2] for r in rows] == ['{"path": "a.txt"}', '{"path": "b.txt"}']


def test_seq_counts_per_step():
    """seq 是"这一步里第几次"，跨步要重新从 0 数。"""
    root = _workdir()
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)

    ckpt.begin_step(0)
    ckpt.log_tool_call('t', 'in0a', 'out0a')
    ckpt.log_tool_call('t', 'in0b', 'out0b')
    ckpt.begin_step(1)
    ckpt.log_tool_call('t', 'in1a', 'out1a')

    with ckpt._connect() as conn:
        rows = conn.execute('SELECT step, seq FROM tool_calls ORDER BY step, seq').fetchall()
    assert rows == [(0, 0), (0, 1), (1, 0)]


def test_rollback_deletes_all_tool_calls_of_rolled_back_step():
    """回滚要删掉这一步的【每一条】工具账（复合主键下别只删到一条）。"""
    root = _workdir()
    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)

    ckpt.begin_step(0)
    ckpt.log_tool_call('t', 'in0a', 'out0a')
    ckpt.log_tool_call('t', 'in0b', 'out0b')
    ckpt.begin_step(1)
    ckpt.log_tool_call('t', 'in1a', 'out1a')

    ckpt.rollback(1)

    with ckpt._connect() as conn:
        rows = conn.execute('SELECT step, seq FROM tool_calls ORDER BY step, seq').fetchall()
    assert rows == [(0, 0), (0, 1)], f'回滚后应该只剩 step 0 的两条，实际 {rows}'


def test_legacy_db_is_migrated_without_losing_data():
    """★ 兼容回归：旧库文件（step 单列主键）要能无损升级，老数据不能丢。

    面试项目可能已经跑出过 .agent/checkpoints.db；schema 换了如果不迁移，
    旧库会一直卡在老结构上，升级后第一次"一步两工具"照样崩。
    """
    root = _workdir()
    db_dir = root / '.agent'
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / 'checkpoints.db')
    conn.executescript("""
        CREATE TABLE tool_calls (
            step   INTEGER PRIMARY KEY,
            tool   TEXT NOT NULL,
            input  TEXT,
            output TEXT,
            ts     TEXT NOT NULL
        );
        INSERT INTO tool_calls VALUES (0, 'old_tool', 'in0', 'out0', '2026-01-01T00:00:00');
        INSERT INTO tool_calls VALUES (2, 'old_tool', 'in2', 'out2', '2026-01-01T00:00:01');
    """)
    conn.commit()
    conn.close()

    mem = ShortTermMemory(max_tokens=8000)
    ckpt = Checkpoint(root, mem.message)      # 构造时触发迁移
    assert ckpt.step == 3, '旧库 MAX(step)=2 → 下一步应该是 3'

    # 迁移后老数据还在，而且同一步能继续记第二条
    ckpt.begin_step(3)
    ckpt.log_tool_call('new_tool', 'in3a', 'out3a')
    ckpt.log_tool_call('new_tool', 'in3b', 'out3b')

    with ckpt._connect() as conn:
        rows = conn.execute(
            'SELECT step, seq, input FROM tool_calls ORDER BY step, seq'
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, 0), (2, 0), (3, 0), (3, 1)]
    assert rows[0][2] == 'in0', '旧库数据被迁移弄丢了'


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
