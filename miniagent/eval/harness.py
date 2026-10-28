"""P12 · 评测 harness —— 跑用例 → 打分 → 出报告。

════════════════════════════════════════════════════════════════════
【一句话】
    P12 只干三件事：
        ① 读题（load_cases）
        ② 跑题 + 判分（run_eval + score）
        ③ 出报告（write_report）

    它【不】负责让 Agent 变好 —— 那是 P13 的事（看报告 → 归因 → 改一处 → 复测）。

════════════════════════════════════════════════════════════════════
【★ 和 tests/ 那 255 条测试有什么区别？】

    ┌──────────────┬────────────────────────┬──────────────────────┐
    │              │ 单元测试（tests/）      │ 评测（本文件）         │
    ├──────────────┼────────────────────────┼──────────────────────┤
    │ 问什么        │ 代码逻辑有没有 bug      │ Agent 答得好不好       │
    │ 判法          │ assert 相等（确定）     │ 统计（10 条对 6 条）   │
    │ 结果稳定吗    │ 稳定                   │ 不稳定（LLM 有随机性） │
    │ 目标          │ 全绿                   │ 数字往上走            │
    └──────────────┴────────────────────────┴──────────────────────┘

    因为 LLM 不确定，所以不能用 assert，只能"跑一批、数一数"。
    这就是评测存在的全部理由。

════════════════════════════════════════════════════════════════════
【用例长什么样】（eval/cases/*.jsonl，一行一条）

    {"input": "2023 年销售额最高的城市是哪个？", "expected": "杭州", "scorer": "contains"}
    {"input": "2023 年总销售额是多少？",          "expected": 3100,   "scorer": "numeric"}

    三个字段：
        input    —— 喂给 Agent 的问题
        expected —— 标准答案（人工预先写好）
        scorer   —— 用哪种判法（可省略，默认 contains）

════════════════════════════════════════════════════════════════════
【三种判法】（为什么不止一种？）

    contains   —— 期望答案【出现在】回答里
                  "答案是 2" 判对（含 "2"）
                  适合：答案会裹在一堆解释里的题

    numeric    —— 数值【足够接近】
                  "总销售额是 3100 元" 判对（抽出 3100）
                  适合：数值题。★ 不能用 == ，因为浮点有精度误差

    set_match  —— 元素一样、顺序随便
                  "2024,2023" 对 "2023,2024" 判对
                  适合："列出所有 X"这种题

════════════════════════════════════════════════════════════════════
【指标】

    accuracy      准确率 = 答对数 / 总数
    avg_steps     平均步数（Agent 循环转了几圈）
    avg_tokens    平均 token（花多少钱）
    latency_p50   中位数延迟（"一半的题比这快"）
    latency_p95   95 分位（"95% 的题比这快" = 最坏情况）
    failures      失败分类（★ 没有它，你只知道"60 分"，不知道改哪里）

    ★ 为什么用 p50/p95 而不是平均值？
        平均值会被极少数【特别慢】的题带偏。
        10 道题耗时 [0.5,0.6,0.6,0.7,0.8,0.9,1.0,1.1,1.2,30.0]
          平均值 = 3.74 秒（被那个 30 秒拉到 3.74，完全不能代表大部分题）
          p50    = 0.85 秒（真实体验）
          p95    = 30.0 秒（诚实暴露"有 5% 的题会卡很久"）
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from miniagent.core.message import Message

# ============================================================================
#  ① 读题
# ============================================================================

def load_cases(path: str | Path) -> List[Dict[str, Any]]:
    """读 .jsonl：一行一个 JSON，每行一道题。

    ★ 为什么用 .jsonl 而不是 .json？
        .jsonl 一行一条，坏一行不影响其他行，而且可以一行一行读（不用一次全塞进内存）。
        .json 一个语法错误整份文件作废。

    ★ 为什么这里要"少一行就报错"，而 LLM 输出要"能宽容就宽容"？
        因为用例文件是你手写的，出错必须立刻发现（fail fast）；
        而 LLM 的输出不受你控制，只能尽量容忍。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'用例文件不存在：{path}')

    cases: List[Dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):        # 空行 / 注释行跳过
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{path.name} 第 {lineno} 行不是合法 JSON：{exc}') from exc

        for key in ('input', 'expected'):
            if key not in case:
                raise ValueError(f'{path.name} 第 {lineno} 行缺少字段 {key!r}')
        case.setdefault('scorer', 'contains')
        cases.append(case)

    if not cases:
        raise ValueError(f'{path} 里一条用例都没有')
    return cases


# ============================================================================
#  ② 判分
# ============================================================================

_NUMBER_RE = re.compile(r'-?\d+(?:\.\d+)?')


def _norm(text: str) -> str:
    """压掉多余空白，避免"杭州 "和"杭州"被判成不同。"""
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def score(answer: str, expected: Any, scorer: str = 'contains') -> bool:
    """判一道题对不对。返回 True/False。

    ★ 判分永远不抛异常 —— 一道题判分崩了不该让整轮评测中断。
    """
    answer = _norm(answer)
    if not answer:
        return False

    try:
        if scorer == 'contains':
            return _norm(expected) in answer

        if scorer == 'numeric':
            target = float(expected)
            for token in _NUMBER_RE.findall(answer):
                got = float(token)
                # 相对误差 1% 或绝对误差 0.01 以内都算对（浮点不能直接比 ==）
                if abs(got - target) <= max(abs(target) * 0.01, 0.01):
                    return True
            return False

        if scorer == 'set_match':
            def to_set(x: Any) -> set:
                if isinstance(x, (list, tuple, set)):
                    return {_norm(str(i)) for i in x}
                return {_norm(i) for i in str(x).split(',') if _norm(i)}
            expected_set = to_set(expected)
            return bool(expected_set) and expected_set == to_set(answer)

    except (TypeError, ValueError):
        return False

    raise ValueError(f'未知的 scorer：{scorer!r}（只有 contains / numeric / set_match）')


# ============================================================================
#  ③ 跑全部用例
# ============================================================================

def _percentile(values: Sequence[float], p: float) -> float:
    """线性插值算分位数。p=0.5 中位数，p=0.95 是 95 分位。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(s[int(k)], 4)
    return round(s[lo] * (hi - k) + s[hi] * (k - lo), 4)


def _count_steps(produced: Sequence[Message]) -> int:
    """数这次 run 转了几圈 = 最大的 step + 1。

    Agent.run 会给每条 assistant 盖章 `reply.step = step`（0 开始）。
    没调工具 → 只有 step=0 的一条 → 1 步。
    """
    steps = [m.step for m in produced if m.step is not None]
    return (max(steps) + 1) if steps else 1


def _rough_tokens(produced: Sequence[Message]) -> int:
    """粗估 token 数：中文按 1 字≈1 token，英文按 4 字符≈1 token。

    ★ 为什么不直接 import estimate_tokens？
        为了这个模块能脱离 core/context 单独跑（它依赖 markdown_it）。
        真要精确，换成 core.context.estimate_tokens 即可 —— 一行的事。
    """
    total = 0
    for m in produced:
        text = str(m.content or '')
        cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        total += cjk + (len(text) - cjk) // 4
    return total


def _classify(ok: bool, answer: str, error: str) -> str:
    """失败分类 —— P13 全靠它才知道"该改哪里"。"""
    if ok:
        return 'passed'
    if error:
        return 'exception'          # Agent 自己抛异常了
    if not _norm(answer):
        return 'empty_answer'       # 跑完了，但一个字没说
    return 'wrong_answer'           # 有答案，但不对


def run_eval(
    agent_factory: Callable[[], Any],
    cases: Sequence[Dict[str, Any]],
    *,
    verbose: bool = True,
) -> Dict[str, Any]:
    """跑全部用例，收集指标。

    Args:
        agent_factory: **零参数**函数，调用一次返回一个【全新的】 Agent。
                       为什么是工厂而不是 Agent 实例？
                         题目之间必须隔离 —— 上一题的对话历史/内存会污染下一题。
                         每题一个白纸 Agent，结果才干净。
        cases:         load_cases() 的结果
        verbose:       打印进度

    Returns:
        dict：指标 + 每道题的明细（rows），可以直接喂给 write_report()。
    """
    rows: List[Dict[str, Any]] = []
    started = time.time()

    for i, case in enumerate(cases, 1):
        agent = agent_factory()          # ★ 每题一个新 Agent
        produced: List[Message] = []
        error = ''
        t0 = time.time()

        try:
            produced = list(agent.run([Message.user(case['input'])]))
        except Exception as exc:                                  # noqa: BLE001
            error = f'{type(exc).__name__}: {exc}'

        elapsed = round(time.time() - t0, 4)
        answer = (produced[-1].content or '') if produced else ''
        ok = (not error) and score(answer, case['expected'], case['scorer'])

        row = {
            'index': i,
            'input': case['input'],
            'expected': case['expected'],
            'answer': answer,
            'scorer': case['scorer'],
            'ok': ok,
            'kind': _classify(ok, answer, error),
            'error': error,
            'steps': _count_steps(produced),
            'tokens': _rough_tokens(produced),
            'elapsed': elapsed,
        }
        rows.append(row)

        if verbose:
            mark = 'ok  ' if ok else 'FAIL'
            print(f'  [{mark}] {i:>2}. {case["input"][:34]:<34} '
                  f'{elapsed:>5.2f}s  答: {answer[:20]!r}')

    passed = sum(1 for r in rows if r['ok'])
    failures: Dict[str, int] = {}
    for r in rows:
        if not r['ok']:
            failures[r['kind']] = failures.get(r['kind'], 0) + 1

    return {
        'total': len(rows),
        'passed': passed,
        'accuracy': round(passed / len(rows), 4) if rows else 0.0,
        'avg_steps': round(sum(r['steps'] for r in rows) / len(rows), 2) if rows else 0.0,
        'avg_tokens': round(sum(r['tokens'] for r in rows) / len(rows), 1) if rows else 0.0,
        'latency_p50': _percentile([r['elapsed'] for r in rows], 0.50),
        'latency_p95': _percentile([r['elapsed'] for r in rows], 0.95),
        'total_elapsed': round(time.time() - started, 2),
        'failures': failures,
        'rows': rows,
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# ============================================================================
#  ④ 出报告
# ============================================================================

def write_report(result: Dict[str, Any], path: str | Path,
                 *, title: str = '评测报告') -> Path:
    """把 run_eval 的结果写成 markdown。返回写出的路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    acc = result['accuracy'] * 100
    lines = [
        f'# {title}',
        '',
        f'> 生成时间：{result["started_at"]}　'
        f'｜　总耗时 {result["total_elapsed"]}s　'
        f'｜　用例 {result["total"]} 条',
        '',
        '## 总体指标',
        '',
        '| 指标 | 值 | 说明 |',
        '|---|---|---|',
        f'| **准确率** | **{acc:.1f}%** ({result["passed"]}/{result["total"]}) | 答对数 / 总数 |',
        f'| 平均步数 | {result["avg_steps"]} | Agent 平均转了几圈 |',
        f'| 平均 token | {result["avg_tokens"]} | 衡量成本 |',
        f'| p50 延迟 | {result["latency_p50"]}s | 一半的题比这快 |',
        f'| p95 延迟 | {result["latency_p95"]}s | 95% 的题比这快（最坏情况） |',
        '',
    ]

    # ---- 失败分类（P13 的入口） ----
    lines += ['## 失败分类', '']
    if result['failures']:
        lines += ['| 类型 | 条数 | 含义 |', '|---|---|---|']
        meaning = {
            'wrong_answer': '跑完了但答错 → 改 prompt / 工具描述',
            'empty_answer': '跑完了但没说话 → 查 Agent 终止条件',
            'exception': 'Agent 抛异常 → 查代码/工具',
        }
        for kind, count in sorted(result['failures'].items(), key=lambda kv: -kv[1]):
            lines.append(f'| `{kind}` | {count} | {meaning.get(kind, "—")} |')
    else:
        lines.append('全部通过。')

    # ---- 失败明细 ----
    failed = [r for r in result['rows'] if not r['ok']]
    lines += ['', '## 失败明细', '']
    if not failed:
        lines.append('（无）')
    for r in failed:
        lines += [
            f'### #{r["index"]}　{r["kind"]}',
            '',
            f'- 问题：{r["input"]}',
            f'- 期望：`{r["expected"]!r}`（判法 `{r["scorer"]}`）',
            f'- 实际：`{r["answer"][:200]!r}`',
            f'- 耗时：{r["elapsed"]}s　步数：{r["steps"]}　token：{r["tokens"]}',
        ]
        if r['error']:
            lines.append(f'- 异常：`{r["error"]}`')
        lines.append('')

    # ---- 全量清单 ----
    lines += ['## 全部用例', '',
              '| # | 问题 | 期望 | 实际 | 结果 | 步数 | 耗时 |',
              '|---|---|---|---|---|---|---|']
    for r in result['rows']:
        got = (r['answer'] or '')[:24].replace('|', '/').replace('\n', ' ')
        lines.append(
            f'| {r["index"]} | {r["input"][:26]} | `{r["expected"]}` | '
            f'{got} | {"✅" if r["ok"] else "❌"} | {r["steps"]} | {r["elapsed"]}s |'
        )

    lines += [
        '',
        '---',
        '',
        '## 下一步（P13）',
        '',
        '1. 从「失败分类」里挑**占比最高的那一类**；',
        '2. 只改**一个**变量（system prompt / 工具 description / max_steps）；',
        '3. 重跑，对比准确率是涨还是跌；',
        '4. 在下面的表格里记录每一次改动 —— 这就是面试能讲的"因果结论"。',
        '',
        '| 轮次 | 改了什么 | 准确率 | 结论 |',
        '|---|---|---|---|',
        f'| 基线 | — | {acc:.1f}% | — |',
        '| 1 | （待填） | （待填） | （待填） |',
        '',
    ]

    path.write_text('\n'.join(lines), encoding='utf-8')
    return path
