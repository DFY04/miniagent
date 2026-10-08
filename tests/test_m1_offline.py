"""M1 离线测试：用「假 LLM」验证 harness 主循环，不需要网络 / API key。

两种跑法都行：

    # 1) pytest（PyCharm 里点绿三角 / 命令行）
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_m1_offline.py -v

    # 2) 当普通脚本跑（不需要 pytest）
    .venv\\Scripts\\python.exe -m miniagent.tests.test_m1_offline

验证 5 件事：
  1. 两级注册表：TOOL_REGISTRY 存「类」，agent.tool_map 存「实例」
  2. 主循环：think -> act -> observe 能连续跑多轮工具调用
  3. 工具真的被执行了（结果里有正确答案）
  4. 模型确实收到了工具说明书
  5. 死循环保护：max_steps 能截断无限调用
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

# 允许 pytest / 脚本两种方式都能 import miniagent
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


# 剧本：先看表结构 -> 再查数据 -> 最后总结
SCRIPT = [
    Message.assistant(function_call=FunctionCall('list_tables', '{}')),
    Message.assistant(function_call=FunctionCall(
        'sql_query', '{"query": "SELECT city FROM sales WHERE year = 2023 ORDER BY amount DESC LIMIT 1"}')),
    Message.assistant('2023 年销售额最高的城市是杭州。'),
]


def _build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE sales (city TEXT, year INTEGER, amount REAL)')
    conn.executemany('INSERT INTO sales VALUES (?,?,?)', [
        ('杭州', 2023, 1200), ('杭州', 2024, 1500),
        ('上海', 2023, 900), ('上海', 2024, 1100),
        ('北京', 2023, 1000), ('北京', 2024, 1050),
    ])
    conn.commit()
    conn.close()


def make_agent(script=None):
    """造一个「假 LLM + 临时 db」的 agent，并跑一轮。返回 (agent, fake, produced)。"""
    db = os.path.join(tempfile.mkdtemp(), 'test.db')
    _build_db(db)
    fake = FakeLLM(script if script is not None else SCRIPT)
    agent = Agent(
        llm=fake,
        system_message='你是数据分析助手',
        tools=[{'name': 'list_tables', 'db': db}, {'name': 'sql_query', 'db': db}],
    )
    produced = list(agent.run([Message.user('2023 年销售额最高的城市是哪个？')]))
    return agent, fake, produced


# ==================== pytest 风格测试 ====================

def test_registry_stores_classes_not_instances():
    """第一级 TOOL_REGISTRY：名字 -> 类。"""
    assert 'sql_query' in TOOL_REGISTRY
    assert isinstance(TOOL_REGISTRY['sql_query'], type)


def test_agent_tool_map_stores_instances():
    """第二级 agent.tool_map：名字 -> 实例（且实例 != 类）。"""
    agent, _, _ = make_agent()
    assert isinstance(agent.tool_map['sql_query'], data_tools.SqlQuery)
    assert agent.tool_map['sql_query'] is not TOOL_REGISTRY['sql_query']


def test_loop_runs_two_tool_rounds():
    """主循环消息序列：assistant -> function -> assistant -> function -> assistant。"""
    _, _, produced = make_agent()
    assert [m.role for m in produced] == ['assistant', 'function', 'assistant', 'function', 'assistant']


def test_final_answer_is_correct():
    """最后一个 assistant 消息是最终答案。"""
    _, _, produced = make_agent()
    assert produced[-1].content.endswith('杭州。')


def test_tools_actually_executed():
    """工具真的跑了（不是伪造的结果）。"""
    _, _, produced = make_agent()
    functions = [m for m in produced if m.role == 'function']
    assert 'sales' in functions[0].content      # list_tables 的结果
    assert '杭州' in functions[1].content        # sql_query 的结果


def test_llm_saw_tool_schemas():
    """模型确实收到了两份工具说明书。"""
    _, fake, _ = make_agent()
    assert set(fake.seen_tool_names) == {'list_tables', 'sql_query'}


def test_max_steps_guard():
    """死循环保护：模型一直要调工具时，会被 max_steps 截断。"""
    loop_script = [Message.assistant(function_call=FunctionCall('list_tables', '{}'))] * 20
    _, _, produced = make_agent(loop_script)
    assert '最大步数' in produced[-1].content


# ==================== 脚本风格入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    for name, fn in tests:
        fn()
        print(f'  ✅ {name}')
    print(f'\n✅ M1 全部 {len(tests)} 个测试通过（离线，无需 LLM）')


if __name__ == '__main__':
    main()
