"""M1 demo：一个会算不定积分的 Agent（真实调用 LLM）。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.examples.m1_data_agent
    .venv\\Scripts\\python.exe -m miniagent.examples.m1_data_agent "x^2 + sin(x) 的不定积分"

依赖：sympy（用 uv pip install --python .venv/Scripts/python.exe sympy 安装）
"""
from __future__ import annotations

import os
import sys
from typing import Union

# 允许 `python miniagent/examples/m1_data_agent.py` 直接执行时也能 import miniagent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sympy as sp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sympy.parsing.sympy_parser import (  # noqa: E402
    convert_xor, implicit_multiplication_application, parse_expr, standard_transformations)

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.core.tool import BaseTool, ToolError, tool  # noqa: E402
from miniagent.llm import LLMClient  # noqa: E402


# ==================== 工具：计算不定积分 ====================

# ① 白名单：允许用户在表达式里出现的函数/常量
_MATH_WHITELIST = {
    'sin': sp.sin, 'cos': sp.cos, 'tan': sp.tan,
    'asin': sp.asin, 'acos': sp.acos, 'atan': sp.atan,
    'sinh': sp.sinh, 'cosh': sp.cosh, 'tanh': sp.tanh,
    'exp': sp.exp, 'log': sp.log, 'ln': sp.log, 'sqrt': sp.sqrt,
    'Abs': sp.Abs, 'pi': sp.pi, 'E': sp.E, 'oo': sp.oo,
}

# ② 解析器内部会用到的名字（auto_number / auto_symbol）
#    ★ '__builtins__': {} 是关键：sympy 解析底层是 eval，不封掉这个就能执行任意代码
_INTERNAL = {
    '__builtins__': {},
    'Symbol': sp.Symbol, 'Integer': sp.Integer, 'Float': sp.Float, 'Rational': sp.Rational,
}

# ③ 容错：x^2 当成 x**2；2x 当成 2*x
_TRANSFORMS = standard_transformations + (convert_xor, implicit_multiplication_application)


@tool('interg')
class Interg(BaseTool):
    """计算不定积分（符号积分），返回原函数 + 常数 C。"""

    description = '计算不定积分（符号积分）。当用户要求求不定积分/原函数时调用此工具。'
    parameters = {
        'type': 'object',
        'properties': {
            'expression': {
                'type': 'string',
                'description': '被积函数表达式，自变量用 x，例如 "x**2 + sin(x)"。',
            },
            'variable': {
                'type': 'string',
                'description': '对哪个变量积分，默认 x。',
            },
        },
        'required': ['expression'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        expr_str = (p.get('expression') or '').strip()
        if not expr_str:
            raise ToolError('expression 不能为空')

        var_name = (p.get('variable') or 'x').strip() or 'x'
        x = sp.Symbol(var_name)

        local = dict(_MATH_WHITELIST)
        local[var_name] = x                       # 让 'x' / 't' 指向符号本身

        # 第 1 步：字符串 → sympy 表达式（安全版）
        try:
            expr = parse_expr(expr_str,
                              local_dict=local,
                              global_dict=dict(_INTERNAL),
                              transformations=_TRANSFORMS)
        except Exception as e:
            raise ToolError(f'无法解析表达式 {expr_str!r}: {e}') from e

        # 第 2 步：真的积分
        try:
            antideriv = sp.integrate(expr, x)
        except Exception as e:
            raise ToolError(f'积分失败: {e}') from e

        # 第 3 步：返回字符串（会被作为 observation 回给模型）
        return f'∫ {sp.sstr(expr)} d{var_name} = {sp.sstr(antideriv)} + C'


# ==================== demo ====================

def main() -> None:
    load_dotenv()

    llm = LLMClient({'model': 'qwen-max'})
    agent = Agent(
        llm=llm,
        system_message='你是一个数学助手。遇到求不定积分的问题，请调用 interg 工具来计算，不要自己心算。',
        tools=['interg'],                       # ← 注册工具（第二级注册表）
    )

    # 可用命令行参数覆盖问题
    question = sys.argv[1] if len(sys.argv) > 1 else 'e的x次幂的不定积分是什么？'
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
