"""数据分析工具：在只读 SQLite 上查数据。

这是一次真实工具（而非玩具加法），也是后面 M5「危险操作审批」的抓手：
sql_query 未来接审批闸门（执行前人工批准），list_tables 则无需审批。
"""
from __future__ import annotations

import sqlite3
from typing import Union

from miniagent.core.tool import BaseTool, ToolError, tool

MAX_ROWS = 50  # 结果最多显示多少行，避免把超长结果塞进上下文


@tool('list_tables')
class ListTables(BaseTool):
    description = '列出数据库里所有表及其列结构。在写 SQL 之前先用它了解表结构。'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.db = self.cfg.get('db')
        if not self.db:
            raise ValueError('list_tables 需要配置 `db`（sqlite 文件路径）')

    def call(self, params: Union[str, dict], **kwargs) -> str:
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            out = []
            for t in tables:
                cur.execute(f'PRAGMA table_info({t})')
                cols = [f'{c[1]}({c[2]})' for c in cur.fetchall()]
                out.append(f'表 {t}: ' + ', '.join(cols))
            return '\n'.join(out) if out else '库里没有表'
        finally:
            conn.close()


@tool('sql_query')
class SqlQuery(BaseTool):
    description = '在数据库上执行一条只读 SELECT 查询并返回结果。回答数据问题时使用。'
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': '要执行的 SQL 查询语句（只允许 SELECT）。',
            }
        },
        'required': ['query'],
    }

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.db = self.cfg.get('db')
        if not self.db:
            raise ValueError('sql_query 需要配置 `db`（sqlite 文件路径）')

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        query = (p.get('query') or '').strip()
        if not query:
            raise ToolError('query 不能为空')
        # M1 只允许 SELECT；真正的「危险操作审批」在 M5 加
        if not query.lower().startswith('select'):
            raise ToolError('只允许 SELECT 查询')

        conn = sqlite3.connect(self.db)
        try:
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            if not rows:
                return '查询结果为空'
            lines = [' | '.join(cols), '-' * 24]
            for r in rows[:MAX_ROWS]:
                lines.append(' | '.join(str(x) for x in r))
            if len(rows) > MAX_ROWS:
                lines.append(f'... 共 {len(rows)} 行，仅显示前 {MAX_ROWS} 行')
            return '\n'.join(lines)
        finally:
            conn.close()
