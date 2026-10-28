"""P12 · 评测 demo —— 全离线，不需要 API key，直接跑。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.eval.demo

它做三件事：
    1. 读 eval/cases/sales.jsonl（10 条题）
    2. 用一个【假 LLM】当被测对象（故意答错 3 条，好让你看到失败分类）
    3. 跑完打印指标 + 写出 eval/report.md

★ 为什么要用假 LLM？
    评测的代码要能【离线跑通】，否则你每改一行 harness 都要花 API 钱。
    换成本项目的真 Agent 只改一行（见文件末尾的注释）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from miniagent.core.agent import Agent
from miniagent.core.message import FunctionCall, Message
from miniagent.core.tool import BaseTool, tool
from miniagent.eval.harness import load_cases, run_eval, write_report

HERE = Path(__file__).resolve().parent
CASES = HERE / 'cases' / 'sales.jsonl'
REPORT = HERE / 'report.md'


# ============================================================================
#  一个假工具：模拟"查数据库"
# ============================================================================

@tool('sales_table')
class SalesTableTool(BaseTool):
    """返回整张销售表 —— 相当于 list_tables + sql_query 的合并简化版。"""

    name = 'sales_table'
    description = '查看销售表的全部内容（city / year / amount）'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params, **kwargs) -> str:
        return ('city | year | amount\n'
                '上海 | 2023 | 900\n'
                '北京 | 2023 | 1000\n'
                '杭州 | 2023 | 1200\n'
                '北京 | 2024 | 1050\n'
                '上海 | 2024 | 1100\n'
                '杭州 | 2024 | 1500')


# ============================================================================
#  假 LLM：按题查表回答；故意留几条错的
# ============================================================================

#: 需要先查表才能答的题（会让 steps 变成 2）
NEEDS_TOOL = {
    '2023 年所有城市的总销售额是多少？',
    '2024 年所有城市的总销售额是多少？',
    '数据库里一共有几个不同的城市？',
    '杭州两年的总销售额是多少？',
    '北京两年的总销售额是多少？',
    '这个数据库记录了哪几年的销售数据？',
}

#: 正确答案
CORRECT = {
    '2023 年销售额最高的城市是哪个？': '2023 年最高的是杭州，1200 万元。',
    '2023 年销售额最低的城市是哪个？': '上海，900 万元。',
    '2024 年销售额最高的城市是哪个？': '杭州，1500 万元。',
    '2024 年销售额最低的城市是哪个？': '北京，1050 万元。',
    '2023 年所有城市的总销售额是多少？': '总销售额是 3100 万元。',
    '2024 年所有城市的总销售额是多少？': '总销售额是 3650 万元。',
    '数据库里一共有几个不同的城市？': '一共 3 个城市：北京、上海、杭州。',
    '杭州两年的总销售额是多少？': '杭州两年合计 2700 万元。',
    '北京两年的总销售额是多少？': '北京两年合计 2050 万元。',
    '这个数据库记录了哪几年的销售数据？': '2024,2023',
}

#: ★ 故意答错的题（为了让报告里有失败分类）
WRONG = {
    '2023 年销售额最低的城市是哪个？': '北京，1000 万元。',      # 错：应是上海
}

#: ★ 故意不说话的题
EMPTY = {'2024 年销售额最高的城市是哪个？'}

#: ★ 故意抛异常的题
BOOM = {'杭州两年的总销售额是多少？'}

#: 模拟网络延迟（真实 LLM 通常 0.5~3 秒；这里缩小到 20ms 使 demo 不磨蹭）
SIMULATED_LATENCY = 0.02


class FakeLLM:
    """每次 chat 都被调用一次；需要查表的题会先发一次 function_call。"""

    def __init__(self):
        self.tool_called = False

    def chat(self, messages, tools=None):
        time.sleep(SIMULATED_LATENCY)          # 假装在等模型回包
        question = next((m.content for m in messages if m.role == 'user'), '')

        # 第一圈：需要数据的题先调工具
        if question in NEEDS_TOOL and not self.tool_called:
            self.tool_called = True
            return Message.assistant(function_call=FunctionCall(
                name='sales_table', arguments='{}'))

        # 第二圈（或不需要工具的题）：直接回答
        if question in BOOM:
            raise RuntimeError('模型超时（演示用）')
        if question in EMPTY:
            return Message.assistant('')
        return Message.assistant(WRONG.get(question, CORRECT.get(question, '我不知道')))


def agent_factory() -> Agent:
    """★ 每题调用一次，返回一个全新的 Agent（题目之间必须隔离）。"""
    return Agent(llm=FakeLLM(), tools=[SalesTableTool()], max_steps=4)


# ============================================================================
#  主流程
# ============================================================================

def main() -> None:
    print('=' * 72)
    print('P12 · 评测 demo（离线假 LLM，故意留 3 条失败）')
    print('=' * 72)

    cases = load_cases(CASES)
    print(f'读入 {len(cases)} 条用例：{CASES.relative_to(HERE.parent)}\n')

    result = run_eval(agent_factory, cases)

    print('\n' + '-' * 72)
    print(f'准确率      {result["accuracy"] * 100:.1f}%  '
          f'({result["passed"]}/{result["total"]})')
    print(f'平均步数    {result["avg_steps"]}')
    print(f'平均 token  {result["avg_tokens"]}')
    print(f'p50 延迟    {result["latency_p50"]}s')
    print(f'p95 延迟    {result["latency_p95"]}s')
    print(f'失败分类    {result["failures"]}')

    path = write_report(result, REPORT, title='P12 评测报告（sales 数据集）')
    print('-' * 72)
    print(f'报告已写入  {path}')

    print('\n' + '=' * 72)
    print('报告的骨架（前 40 行）：')
    print('=' * 72)
    for line in path.read_text(encoding='utf-8').splitlines()[:40]:
        print(line)


if __name__ == '__main__':
    main()


# ============================================================================
#  换成真 Agent（联网、要 DASHSCOPE_API_KEY）
# ============================================================================
#     from miniagent.llm import LLMClient        # 你项目里的适配器
#     from miniagent.tools.data_tools import ... # list_tables / sql_query
#
#     def agent_factory():
#         return Agent(
#             llm=LLMClient(model='qwen-max'),
#             system_message='你是数据分析助手。查数据请用 list_tables / sql_query。',
#             tools=['list_tables', 'sql_query'],
#             max_steps=6,
#         )
#
#     result = run_eval(agent_factory, load_cases(CASES))
#     write_report(result, REPORT)
