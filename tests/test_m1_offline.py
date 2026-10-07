"""M1 离线测试：用「假 LLM」验证 harness 主循环，不需要网络 / API key。

跑法（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.tests.test_m1_offline

它验证三件事：
  1. 两级注册表：TOOL_REGISTRY 里存「类」，Agent.tool_map 里存「实例」
  2. 主循环：think -> act -> observe 能连续跑两轮工具调用
  3. 工具真的被执行了（sql_query 查出的结果里有正确答案）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.core.tool import TOOL_REGISTRY  # noqa: E402
from miniagent.tools import data_tools  # noqa: E402,F401


class FakeLLM:
    """按剧本返回固定的 assistant 消息，模拟模型一步步调用工具。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.seen_tool_names = []

    def chat(self, messages, tools=None):
        # 记录模型看到的工具清单，供断言
        self.seen_tool_names = [t['name'] for t in (tools or [])]
        out = self.script[self.i]
        self.i += 1
        return out


def build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE sales (city TEXT, year INTEGER, amount REAL)')
    conn.executemany('INSERT INTO sales VALUES (?,?,?)', [
        ('杭州', 2023, 1200), ('杭州', 2024, 1500),
        ('上海', 2023, 900), ('上海', 2024, 1100),
        ('北京', 2023, 1000), ('北京', 2024, 1050),
    ])
    conn.commit()
    conn.close()


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    db = os.path.join(tmpdir, 'test.db')
    build_db(db)

    # ---- 断言 1：两级注册表 ----
    assert 'sql_query' in TOOL_REGISTRY, 'sql_query 未注册进全局 TOOL_REGISTRY'
    assert isinstance(TOOL_REGISTRY['sql_query'], type), 'TOOL_REGISTRY 里存的应该是「类」'

    # ---- 剧本：先看表结构，再查数据，最后总结 ----
    script = [
        Message.assistant(function_call=FunctionCall('list_tables', '{}')),
        Message.assistant(function_call=FunctionCall(
            'sql_query', '{"query": "SELECT city FROM sales WHERE year = 2023 ORDER BY amount DESC LIMIT 1"}')),
        Message.assistant('2023 年销售额最高的城市是杭州。'),
    ]
    fake = FakeLLM(script)

    agent = Agent(
        llm=fake,
        system_message='你是数据分析助手',
        tools=[{'name': 'list_tables', 'db': db}, {'name': 'sql_query', 'db': db}],
    )

    # ---- 断言 2：tool_map 里存的是「实例」 ----
    assert isinstance(agent.tool_map['sql_query'], data_tools.SqlQuery), 'tool_map 里应该是工具实例'
    assert agent.tool_map['sql_query'] is not TOOL_REGISTRY['sql_query'], '实例 != 类'

    # ---- 跑主循环，收集所有产出的消息 ----
    produced = list(agent.run([Message.user('2023 年销售额最高的城市是哪个？')]))

    roles = [m.role for m in produced]
    print('产生消息序列:', roles)
    print('最终答案:', produced[-1].content)

    # ---- 断言 3：主循环连续跑了两轮工具调用 ----
    assert roles == ['assistant', 'function', 'assistant', 'function', 'assistant'], f'消息序列不符: {roles}'
    assert produced[-1].content.endswith('杭州。'), '最终答案不对'

    # ---- 断言 4：工具真的被执行了（不是伪造的结果）----
    fn_results = [m for m in produced if m.role == 'function']
    assert fn_results[0].name == 'list_tables' and 'sales' in fn_results[0].content
    assert '杭州' in fn_results[1].content, 'sql_query 的结果里应包含正确答案'

    # ---- 断言 5：模型确实拿到了工具说明书 ----
    assert set(fake.seen_tool_names) == {'list_tables', 'sql_query'}

    print('\n✅ M1 全部断言通过（离线，无需 LLM）')


if __name__ == '__main__':
    main()
