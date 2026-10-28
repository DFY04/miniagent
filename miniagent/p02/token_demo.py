"""token 估算演示：为什么上下文需要「预算」。

独立版不再依赖 Qwen 官方分词器（那是 qwen-agent 的依赖），改用 core/context.py
里的粗略估算：中文 1 字算 1 token，其他字符 4 个算 1 token。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe -m miniagent.p02.token_demo
"""
from miniagent.core.context import estimate_tokens


def main() -> None:
    样例 = [
        "你好",
        "用一句话解释什么是 REST API。",
        "The quick brown fox jumps over the lazy dog.",
        "这是一段中文和 English 混在一起的文字，用来看看估算差多少。",
    ]

    print(f"{'文字':<50}{'字符数':>8}{'估算 token':>12}")
    print("-" * 72)
    for 文字 in 样例:
        print(f"{文字[:46]:<50}{len(文字):>8}{estimate_tokens(文字):>12}")

    print()
    print("估算规则：中文 1 字 = 1 token，其他字符 4 个 = 1 token。")
    print("为什么要估算：模型一次能看的 token 有上限；超了要截断或压缩（见 core/context.py）。")
    print("为什么不用真分词器：多一个依赖，而且这里只需要判断「超没超预算」，粗估够用。")


if __name__ == "__main__":
    main()
