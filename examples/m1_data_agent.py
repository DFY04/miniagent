"""M1 demo：一个能查数据库的数据分析 Agent（真实调用 LLM）。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.m1_data_agent
"""
from __future__ import annotations

import os
import sqlite3
import sys

# 允许 `python miniagent/examples/m1_data_agent.py` 直接执行时也能 import miniagent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv  # noqa: E402

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.llm import LLMClient  # noqa: E402
from miniagent.tools import data_tools  # noqa: E402,F401  触发 @tool 注册

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sales_demo.db')


def build_demo_db() -> None:
    """造一个 6 行的销售 demo 库。每次运行重建，保证结果可复现。"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE sales (city TEXT, year INTEGER, amount REAL)')
    conn.executemany('INSERT INTO sales VALUES (?,?,?)', [
        ('杭州', 2023, 1200), ('杭州', 2024, 1500),
        ('上海', 2023, 900), ('上海', 2024, 1100),
        ('北京', 2023, 1000), ('北京', 2024, 1050),
    ])
    conn.commit()
    conn.close()


def main() -> None:
    load_dotenv()
    build_demo_db()

    llm = LLMClient({'model': 'qwen-max'})
    agent = Agent(
        llm=llm,
        system_message=('你是一个数据分析助手。先用 list_tables 了解表结构，'
                        '再用 sql_query 查询来回答问题。不要自己心算。'),
        tools=[
            {'name': 'list_tables', 'db': DB_PATH},
            {'name': 'sql_query', 'db': DB_PATH},
        ],
    )

    # 可用命令行参数覆盖问题
    question = sys.argv[1] if len(sys.argv) > 1 else '2023 年销售额最高的城市是哪个？'
    messages = [Message.user(question)]

    print(f'用户: {question}\n')
    for msg in agent.run(messages):
        if msg.function_call:
            print(f'  [调用工具] {msg.function_call.name}({msg.function_call.arguments})')
        elif msg.role == 'function':
            print(f'  [工具结果] {msg.name}:\n{_indent(msg.content)}')
        elif msg.role == 'assistant':
            print(f'\n助手: {msg.content}')


def _indent(text: str, prefix: str = '    ') -> str:
    return '\n'.join(prefix + line for line in text.splitlines())


if __name__ == '__main__':
    main()
