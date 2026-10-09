"""精确计算 token 数 —— 用 Qwen 官方 tokenizer。

运行（在 qwen-agent 仓库根目录下）：
    .venv\\Scripts\\python.exe -m miniagent.p02.token_demo

重点：qwen-agent 自带 Qwen 的 tokenizer（词表文件就在仓库里，不用下载）。
    from qwen_agent.utils.tokenization_qwen import count_tokens, tokenizer
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from qwen_agent.utils.tokenization_qwen import count_tokens, tokenizer  # noqa: E402


SAMPLES = [
    ('纯中文短', '你好'),
    ('纯中文长', '这是一段中文测试文本，用来对比估算和真实 token 数。'),
    ('纯英文', 'Hello world, this is a test.'),
    ('英文长句', 'The quick brown fox jumps over the lazy dog and runs away.'),
    ('Python 代码', 'def add(a, b):\n    return a + b'),
    ('中英混合', '请帮我计算 1 + 2 等于多少，thanks'),
    ('JSON', '{"query": "SELECT * FROM sales", "limit": 10}'),
]


def demo_count() -> None:
    print('=== 精确 token 数（Qwen 官方 tokenizer）===')
    print(f'{"类型":<12} {"字符数":>6} {"token 数":>8}')
    print('-' * 30)
    for name, s in SAMPLES:
        print(f'{name:<12} {len(s):>6} {count_tokens(s):>8}')

    print()
    print('⚠️ 为什么不能直接数长度：')
    for name, s in SAMPLES:
        ratio = count_tokens(s) / len(s)
        print(f'  {name:<12} len={len(s):>3}  token={count_tokens(s):>3}   token/字符 = {ratio:.2f}')


def demo_encode() -> None:
    print()
    print('=== 一个句子被切成什么样（看 token id 和还原）===')
    for s in ['你好，世界', 'Hello world', '请帮我计算 1 + 2']:
        ids = tokenizer.encode(s)
        pieces = [tokenizer._decode([i]) for i in ids]
        print(f'  原文   : {s!r}')
        print(f'  token数: {len(ids)}')
        print(f'  ids    : {ids}')
        print(f'  还原   : {pieces}')

    print()
    print(f'  词表大小: {tokenizer.vocab_size} 个 token')


def demo_truncate() -> None:
    print()
    print('=== qwen-agent 自带的「按 token 截断」===')
    long_text = '这是一段很长的中文文本，' * 20
    print(f'  原文长度        : {len(long_text)} 字符 / {count_tokens(long_text)} token')

    short = tokenizer.truncate(long_text, max_token=20)
    print(f'  截到 20 token   : {len(short)} 字符 / {count_tokens(short)} token')
    print(f'  截出来          : {short!r}')

    both = tokenizer.truncate(long_text, max_token=20, keep_both_sides=True)
    print(f'  保头保尾 20token: {both!r}')


def demo_estimate_vs_exact() -> None:
    """顺便对比一下粗估（P02 用的）和精确值差多少。"""
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        other = len(text) - cjk
        return cjk + (other + 3) // 4

    print()
    print('=== 粗估 vs 精确 ===')
    print(f'{"类型":<12} {"粗估":>6} {"精确":>6} {"误差":>8}')
    print('-' * 36)
    for name, s in SAMPLES:
        est, real = estimate_tokens(s), count_tokens(s)
        print(f'{name:<12} {est:>6} {real:>6} {(est - real) / real * 100:>+7.0f}%')
    print()
    print('  粗估误差 ±30% —— 对"要不要裁上下文"这种判断完全够用，')
    print('  而且不用加载 6MB 词表。P02 的 truncate 就是用粗估。')


def main() -> None:
    demo_count()
    demo_encode()
    demo_truncate()
    demo_estimate_vs_exact()


if __name__ == '__main__':
    main()
