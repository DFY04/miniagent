"""P12 · 评测（eval）—— 跑用例 → 打分 → 出报告。

    load_cases(path)                  读 .jsonl 用例
    score(answer, expected, scorer)   判一道题对错
    run_eval(agent_factory, cases)    跑全部用例，收指标
    write_report(result, path)        出 report.md

★ 这个包只负责"量"，不负责"改"。改是 P13 的事。
"""
from miniagent.eval.harness import load_cases, run_eval, score, write_report

__all__ = ['load_cases', 'score', 'run_eval', 'write_report']
