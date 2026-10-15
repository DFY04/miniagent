"""test_p05_tracer.py —— P05 验收测试（全离线、零网络、零花费）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p05_tracer.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p05_tracer

验收标准（DAILY_PLAN 里写的那两条 + 几个必要的边界）：
    [x] 一次 run 生成的 JSONL 行数正确
    [x] replay 能按原顺序还原事件
    [+] 每条记录字段齐（ts / type / trace_id / data）
    [+] llm_end 带 latency_ms
    [+] 同一 run 内 trace_id 相同，不同 run 之间不同   ← 最容易写错的一条
    [+] 事件负载里有 Message 也能写进去（不能抛 TypeError）
    [+] 中文不被转义成 \\uXXXX
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.hooks import EventBus, EventType  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.runtime.tracer import Tracer, read_records, replay  # noqa: E402

_DIR = tempfile.mkdtemp(prefix='p05_tracer_')


def _path(name: str) -> str:
    p = os.path.join(_DIR, name + '.jsonl')
    if os.path.exists(p):
        os.remove(p)          # 每个用例都从空文件开始，免得上一次的行混进来
    return p


def _tracer(name: str):
    """建 Tracer + 挂到新总线上，返回 (tracer, bus, path)。"""
    path = _path(name)
    bus = EventBus()
    tracer = Tracer(path)
    tracer.attach(bus)
    return tracer, bus, path


# ==================== 基本：行数 / 字段 ====================

def test_line_count_matches_event_count():
    """★ 一次 run 生成的行数 = 发出的事件数。"""
    tracer, bus, path = _tracer('count')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    bus.emit(EventType.LLM_END, reply='回复')
    bus.emit(EventType.RUN_END, reason='done')
    tracer.close()

    records = read_records(path)
    assert len(records) == 4
    assert [r['type'] for r in records] == ['run_start', 'llm_start', 'llm_end', 'run_end']


def test_every_record_has_required_fields():
    tracer, bus, path = _tracer('fields')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.TOOL_START, name='now')
    tracer.close()

    for rec in read_records(path):
        for key in ('ts', 'type', 'trace_id', 'data'):
            assert key in rec, f'缺字段 {key}'


def test_data_payload_survives():
    """事件负载要原样保留（step / name / result 这些细节）。"""
    tracer, bus, path = _tracer('payload')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.TOOL_END, name='add', result='123 + 456 = 579')
    tracer.close()

    tool_rec = read_records(path)[1]
    assert tool_rec['data']['name'] == 'add'
    assert tool_rec['data']['result'] == '123 + 456 = 579'


# ==================== trace_id ====================

def test_trace_id_is_shared_inside_one_run():
    tracer, bus, path = _tracer('tid_same')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    bus.emit(EventType.RUN_END, reason='done')
    tracer.close()

    ids = {r['trace_id'] for r in read_records(path)}
    assert len(ids) == 1, f'同一次 run 应该只有一个 trace_id，实际 {ids}'
    assert ids != {'unknown'}, 'RUN_START 之后必须生成新 id'


def test_trace_id_changes_between_runs():
    """★★ 最重要的一条：第二次 run 必须换新 id。

    写成"没有 id 才生成"的话，多轮对话的几次 run 会共用一个 id，
    分组功能等于没做 —— 而且不报错。
    """
    tracer, bus, path = _tracer('tid_change')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.RUN_END, reason='done')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.RUN_END, reason='done')
    tracer.close()

    records = read_records(path)
    assert records[0]['trace_id'] != records[2]['trace_id']


# ==================== 耗时 ====================

def test_llm_end_has_latency():
    tracer, bus, path = _tracer('latency')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    time.sleep(0.05)                      # 假装模型想了 50ms
    bus.emit(EventType.LLM_END, reply='回复')
    tracer.close()

    records = read_records(path)
    llm_end = [r for r in records if r['type'] == 'llm_end'][0]
    assert 'latency_ms' in llm_end
    assert llm_end['latency_ms'] >= 50, llm_end['latency_ms']


def test_other_events_have_no_latency():
    """只有 llm_end 该有耗时，别见谁都加。"""
    tracer, bus, path = _tracer('latency_only')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.RUN_END, reason='done')
    tracer.close()

    assert all('latency_ms' not in r for r in read_records(path))


# ==================== 序列化边界 ====================

def test_message_payload_is_serializable():
    """★ 事件负载里有 Message（dataclass）时不能抛 TypeError。"""
    tracer, bus, path = _tracer('message')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_END, reply=Message.assistant('你好'))
    tracer.close()

    rec = read_records(path)[1]
    assert rec['data']['reply']['role'] == 'assistant'
    assert rec['data']['reply']['content'] == '你好'


def test_chinese_is_not_escaped():
    """ensure_ascii=False：中文原样写，别变 \\u4f60\\u597d。"""
    tracer, bus, path = _tracer('chinese')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_END, reply='你好，丁凤阳')
    tracer.close()

    raw = open(path, encoding='utf-8').read()
    assert '你好，丁凤阳' in raw
    assert '\\u' not in raw


# ==================== 回放 ====================

def test_replay_keeps_original_order():
    """★ replay 按原顺序还原。"""
    tracer, bus, path = _tracer('replay')
    for ev, kw in [(EventType.RUN_START, {}),
                   (EventType.LLM_START, {'step': 0}),
                   (EventType.TOOL_START, {'name': 'now'}),
                   (EventType.TOOL_END, {'name': 'now', 'result': '12:00'}),
                   (EventType.RUN_END, {'reason': 'done'})]:
        bus.emit(ev, **kw)
    tracer.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        replay(path)
    out = buf.getvalue()

    order = [name for name in ['run_start', 'llm_start', 'tool_start', 'tool_end', 'run_end']
             if name in out]
    assert order == ['run_start', 'llm_start', 'tool_start', 'tool_end', 'run_end'], out
    # 顺序要对：位置逐个递增
    positions = [out.index(name) for name in order]
    assert positions == sorted(positions)


def test_replay_can_filter_by_trace_id():
    tracer, bus, path = _tracer('replay_filter')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    first_id = read_records(path)[0]['trace_id']
    bus.emit(EventType.RUN_END, reason='done')
    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    tracer.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        replay(path, trace_id=first_id)
    out = buf.getvalue()
    assert out.count('llm_start') == 1     # 只回放第一次 run
    assert out.count('run_start') == 1


def test_replay_empty_file_does_not_crash():
    path = _path('empty')
    open(path, 'w').close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        replay(path)                       # 不该抛异常
    assert '空文件' in buf.getvalue()


# ==================== 总线侧：通配订阅者要能拿到事件类型 ====================

def test_wildcard_subscriber_receives_event_type():
    """★ P05 的前提条件：hooks.py 的 emit 必须把 event 传给通配订阅者。"""
    seen = []
    bus = EventBus()
    bus.subscribe_all(lambda event=None, **kw: seen.append((event, kw)))
    bus.emit(EventType.TOOL_START, name='now')

    assert seen[0][0] is EventType.TOOL_START
    assert seen[0][1] == {'name': 'now'}


def test_old_style_wildcard_handler_still_works():
    """老写法（只收 **kw）不该因为这次修改而崩。"""
    seen = []
    bus = EventBus()
    bus.subscribe_all(lambda **kw: seen.append(kw))
    bus.emit(EventType.RUN_START)

    assert len(seen) == 1
    assert seen[0]['event'] is EventType.RUN_START


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
