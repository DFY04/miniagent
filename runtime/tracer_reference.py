"""P05 —— Tracer：把事件落成 JSONL，然后可以回放。

【要解决什么问题】
    之前事件只能 print，进程一关全丢。想查"这轮为什么慢""为什么调了 3 次工具"
    就只能靠当时的屏幕输出 —— 过了就没了。

    Tracer 干的事很简单：它就是再挂一个订阅者上去，但这个订阅者不 print，
    而是往文件里追加一行 JSON。**Agent 和 EventBus 一行都不用改。**

【数据长什么样（JSONL = 一行一个 JSON 对象）】
    {"ts": 1760000000.1, "type": "run_start", "trace_id": "a1b2c3d4", "data": {}}
    {"ts": 1760000000.2, "type": "llm_start", "trace_id": "a1b2c3d4", "data": {"step": 0}}
    ...                                                                     ↑ latency_ms
    {"ts": 1760000003.7, "type": "llm_end", "trace_id": "a1b2c3d4", "data": {...}, "latency_ms": 3500.2}

    为什么不用"一个大 JSON 数组"？因为追加一行就能写、崩溃时已写的行还在、
    tail/grep 直接可用、读的时候不用全塞进内存。

【字段含义】
    ts         时间戳（time.time()，给人看的"什么时候"）
    type       事件类型（'run_start' / 'llm_end' ...，取自 EventType 的值）
    trace_id   ★ 一次 run 的身份证：同一轮的所有事件共用它，用来分组
    data       事件负载（step / name / result / hits ...）
    latency_ms 只有 llm_end 有：这次模型调用花了多少毫秒
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from miniagent.core.hooks import EventType


# ============================================================
#  json.dumps 的兜底转换器
# ============================================================
def _jsonable(obj: Any) -> Any:
    """json.dumps 遇到"不认识的类型"时会调这个函数，把对象转成能序列化的东西。

    ★ 为什么必须要它：事件负载里带的是 Message / FunctionCall（dataclass），
      不加这个，json.dumps 直接报
          TypeError: Object of type Message is not JSON serializable
      （实测过，就是在 llm_end 那一行炸的。）
    """
    if hasattr(obj, 'to_dict'):      # Message / FunctionCall 都有
        return obj.to_dict()
    if hasattr(obj, 'hex'):          # 保险起见给 UUID 之类留个口子
        return obj.hex
    return str(obj)                  # 最后的兜底：转成字符串，绝不抛异常


# ============================================================
#  Tracer
# ============================================================
class Tracer:
    """订阅总线上的一切事件，一条一行写进 JSONL。

    用法：
        tracer = Tracer('trace.jsonl')
        tracer.attach(bus)          # ← 这一步才真正挂上去
        ...
        tracer.close()              # ★ 用完关掉，别让文件句柄悬着

    为什么用 attach(bus) 而不是在 __init__ 里就订阅？
        ① 构造只装配、不干活（和 add_documents 藏在构造函数里是同一个道理）；
        ② 可以先建 Tracer 再建 bus；
        ③ 测试时能塞个假 bus 进去。
    """

    def __init__(self, path: str):
        self.path = path
        self._trace_id: str = 'unknown'                  # 兜底：第一个事件不是 RUN_START 时用
        self._llm_start: Optional[float] = None          # LLM_START 的时刻，用于算耗时
        # 追加模式 + utf-8：多次 run 共用一个文件，靠 trace_id 区分
        self._file = open(path, 'a', encoding='utf-8')

    # ---------------- 挂到总线上 ----------------
    def attach(self, bus) -> None:
        """把 self._on_event 登记成"所有事件"的订阅者。

        ★ 注意 self._on_event 后面【没有括号】：
          带括号 = 立刻调用，把返回的 None 登记进去（然后就静默失效了）。
        """
        bus.subscribe_all(self._on_event)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    # ---------------- 回调：把事件变成一条记录 ----------------
    def _on_event(self, event: EventType, **data) -> None:
        """总线每发一个事件就会调这个（签名里的 event 靠 hooks.py 传进来）。"""
        # ① 每次 run 开始 → 换一个新的 trace_id
        #    必须"每次换"，不能"没有才生成"：否则多轮对话的几次 run 会共用一个 id，
        #    分组功能等于没做（而且不报错，静默失效）。
        if event is EventType.RUN_START:
            self._trace_id = uuid.uuid4().hex[:8]
            self._llm_start = None

        rec: Dict[str, Any] = {
            'ts': time.time(),
            'type': event.value,          # Enum 的值就是 'run_start' 这种字符串
            'trace_id': self._trace_id,
            'data': data,
        }

        # ② 耗时：LLM_START 只记时刻（不写字段），LLM_END 时算差值
        #    必须用 perf_counter（单调时钟）而不是 time.time()（会被系统对时跳变）
        if event is EventType.LLM_START:
            self._llm_start = time.perf_counter()
        elif event is EventType.LLM_END and self._llm_start is not None:
            rec['latency_ms'] = round((time.perf_counter() - self._llm_start) * 1000, 1)
            self._llm_start = None

        self._write(rec)

    def _write(self, rec: Dict[str, Any]) -> None:
        # ensure_ascii=False：中文原样写，不然会变成 \u4e0a\u6d77，翻日志时眼睛疼
        line = json.dumps(rec, ensure_ascii=False, default=_jsonable)
        self._file.write(line + '\n')
        self._file.flush()      # ★ 立刻落盘：进程崩了也不丢已经写过的事件


# ============================================================
#  读 + 回放
# ============================================================
def read_records(path: str) -> List[Dict[str, Any]]:
    """把 JSONL 读成 dict 列表（按文件里的原顺序）。

    单独抽出来是为了：replay 要用它，测试也要用它（不用重复写读文件逻辑）。
    """
    records: List[Dict[str, Any]] = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue                     # 容忍空行
            records.append(json.loads(line))
    return records


def replay(path: str, trace_id: Optional[str] = None) -> None:
    """读 JSONL，按原顺序把事件序列打印出来。

    Args:
        path: trace 文件
        trace_id: 只想看某一次 run 时传它（文件里通常有很多次 run）
    """
    records = read_records(path)
    if not records:
        print(f'(空文件: {path})')
        return

    # 以文件里第一条记录的时间为 0 点，后面都显示"距开始多少毫秒"，
    # 这样一眼能看出每个阶段花了多久（trace 的价值一半在这里）
    t0 = records[0]['ts']
    for rec in records:
        if trace_id is not None and rec.get('trace_id') != trace_id:
            continue
        dt = (rec['ts'] - t0) * 1000
        line = (f'[+{dt:9.1f}ms] {rec.get("trace_id", "?"):<9} '
                f'{rec.get("type", "?"):<10} {rec.get("data", {})}')
        if 'latency_ms' in rec:
            line += f'   ← 耗时 {rec["latency_ms"]}ms'
        print(line)


if __name__ == '__main__':
    # 小自测：直接跑这个文件，看一遍"写 + 读 + 回放"。
    #   在项目根目录：.venv\Scripts\python.exe -m miniagent.runtime.tracer
    import os
    import tempfile

    from miniagent.core.hooks import EventBus

    tmp = os.path.join(tempfile.gettempdir(), 'tracer_selftest.jsonl')
    if os.path.exists(tmp):
        os.remove(tmp)

    bus = EventBus()
    tracer = Tracer(tmp)
    tracer.attach(bus)

    bus.emit(EventType.RUN_START)
    bus.emit(EventType.LLM_START, step=0)
    time.sleep(0.05)
    bus.emit(EventType.LLM_END, reply='（假装这是模型回复）')
    bus.emit(EventType.RUN_END, reason='done')
    tracer.close()

    print(f'写入 {tmp}：')
    print(open(tmp, encoding='utf-8').read())
    print('回放：')
    replay(tmp)
