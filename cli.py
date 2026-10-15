"""P08 —— 统一 CLI：一个入口跑所有事。

用法（在项目根目录）：

    python -m miniagent.cli --help                              # 看菜单
    python -m miniagent.cli run "丁凤阳是哪里人"                  # 跑一轮 Agent
    python -m miniagent.cli run "文档该怎么切分" --rag miniagent/memory/asn.txt --trace trace.jsonl
    python -m miniagent.cli replay trace.jsonl                  # 回放 trace
    python -m miniagent.cli eval                                # 评测（占位，M6 再接）

【这个文件的结构 —— CLI 代码的标准三段式】

    ① build_parser()  ── 只负责"描述菜单"：有哪些子命令、每个要什么参数。
                          它不干活，只声明。argparse 会据此生成 --help 和报错。
    ② cmd_xxx(args)   ── 每道菜的实现。参数已经解析好了，直接用 args.xxx。
    ③ main()          ── 解析 → 分派（args.func(args)）→ 返回退出码。

    ★ "解析" 和 "干活" 必须分开。混在一起的话，参数一多就没法读、没法测。
    ★ 本文件里把「重活」（import LLM、建库）都放在函数内部，
      这样 `--help` 不用真的去连模型、不会有副作用 —— 这叫"延迟导入"。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# 这个文件的上一级目录 = miniagent 包所在目录（用来拼绝对路径，避免受 cwd 影响）
PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _similar_files(path: str, n: int = 3):
    """给一个打错的路径，猜猜用户想要哪个文件（基于文件名相似度）。

    为什么值得写：路径打错是 CLI 上最高频的错误（少一个字母、写错扩展名…）。
    与其只报“找不到”，不如直接告诉他“你是不是想写 xxx” —— 体验差很多。
    """
    import difflib

    folder = os.path.dirname(path) or '.'
    base = os.path.basename(path)
    if not os.path.isdir(folder):
        return []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, m) for m in difflib.get_close_matches(base, names, n=n, cutoff=0.6)]


# ============================================================
#  ① 描述菜单
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='miniagent',
        description='MiniAgent 统一入口：跑 agent / 回放 trace / 跑评测',
    )
    sub = p.add_subparsers(dest='cmd', required=True)      # 必须给一个子命令

    # ---------------- run：跑一轮 Agent ----------------
    r = sub.add_parser('run', help='跑一轮 Agent（会真的调模型）')
    r.add_argument('question', help='要问的问题')
    r.add_argument('--model', default='qwen-max', help='模型名（默认 qwen-max）')
    r.add_argument('--system', dest='system_message',
                   default='你是一个智能助手。回答时优先依据检索到的参考资料；资料里没有就说不知道。',
                   help='system 人设')
    r.add_argument('--max-steps', type=int, default=10, help='最多几步（默认 10）')
    r.add_argument('--tools', default='now,add', help='挂哪些工具，逗号分隔（默认 now,add）')
    r.add_argument('--rag', metavar='TXT', default=None,
                   help='知识库文件；给了就启用长期记忆（例：miniagent/memory/asn.txt）')
    r.add_argument('--max-retries', type=int, default=2, help='LLM 调用失败重试次数（默认 2）')
    r.add_argument('--loop-window', type=int, default=3,
                   help='死循环检测窗口：连续 N 次同样调用就中止（0 = 不检测）')
    r.add_argument('--max-tokens', type=int, default=None, help='预算：这轮最多花多少 token')
    r.add_argument('--trace', metavar='JSONL', default=None,
                   help='把事件写到这个文件（例：--trace trace.jsonl）')
    r.add_argument('--approve', choices=['ask', 'auto', 'deny'], default='ask',
                   help='危险操作（DROP/rm/sudo…）怎么办：ask=每次问我（默认）、auto=自动批准、deny=一律拒绝')
    r.add_argument('--verbose', action='store_true', help='把每一步事件也打出来')
    r.add_argument('--workdir', metavar='DIR', default=None,
                   help='工作目录：给了就启用 Checkpoint（文件+上下文可回滚），不传 = 关掉')
    r.add_argument('--rollback', choices=['ask', 'never'], default='never',
                   help='每步结束要不要问你“是否回滚”（需配合 --workdir）')
    r.set_defaults(func=cmd_run)

    # ---------------- replay：回放 trace ----------------
    s = sub.add_parser('replay', help='回放一次 trace（按时间线打印）')
    s.add_argument('path', help='trace 文件（JSONL）')
    s.add_argument('--trace-id', default=None, help='只看某一次 run（文件里有很多次时用）')
    s.set_defaults(func=cmd_replay)

    # ---------------- eval：评测（占位） ----------------
    e = sub.add_parser('eval', help='跑评测（M6 再接，现在只占位）')
    e.set_defaults(func=cmd_eval)

    return p


# ============================================================
#  ② 每道菜的实现
# ============================================================
def cmd_run(args) -> int:
    """跑一轮 Agent：装配所有零件 → 跑 → 打印结果。"""
    # 延迟导入：这些 import 有副作用（注册工具）或需要网络/密钥，别在 --help 时就执行
    from dotenv import load_dotenv
    load_dotenv()                                     # 读 .env 里的 DASHSCOPE_API_KEY

    # ★ 先校验输入（便宜），再装配零件（贵）——“参数错了就别去连模型”
    #   路径打错是最高频的错，这里直接给“猜你想要”的提示。
    if args.rag and not os.path.exists(args.rag):
        print(f'找不到知识库文件: {args.rag}', file=sys.stderr)
        for guess in _similar_files(args.rag):
            print(f'  你是不是想写: {guess}', file=sys.stderr)
        return 1

    from miniagent.core.hooks import EventBus, EventType
    from miniagent.core.mem_agent import Agent
    from miniagent.core.message import Message
    from miniagent.llm import LLMClient
    from miniagent.memory.short_term import ShortTermMemory
    from miniagent.runtime.budget import Budget
    from miniagent.runtime.retry import LoopDetector
    from miniagent.runtime.tracer import Tracer
    from miniagent.tools import demo_tools     # noqa: F401  import 一下，now/add 才会注册

    bus = EventBus()

    # 想把每一步事件打出来看，就把这一行打开（默认只打印对话结果）
    if args.verbose:
        bus.subscribe_all(lambda **kw: print(f'    [事件] {kw}'))

    # ---- Tracer：可选。给了 --trace 就把事件落盘 ----
    tracer = None
    if args.trace:
        tracer = Tracer(args.trace)
        tracer.attach(bus)

    # ---- 模型 ----
    llm = LLMClient({'model': args.model})

    # ---- 短期记忆（把 llm 传进去，超预算时能做摘要压缩）----
    mem = ShortTermMemory(max_tokens=8000, llm=llm)

    # ---- 长期记忆：只有给了 --rag 才启用（文件存在性已在开头校验过）----
    rag = None
    if args.rag:
        from miniagent.memory.vector_store import DashScopeChromaRAG
        stem = os.path.splitext(os.path.basename(args.rag))[0]
        rag = DashScopeChromaRAG(
            collection_name=f'cli_{stem}',
            file_path=args.rag,
            # ★ 固定成包内绝对路径：默认的 './chroma_db' 会跟着 cwd 走，
            #   在项目根跑和在别的目录跑会变成两个库（踩过这个坑）。
            persist_path=os.path.join(PKG_DIR, 'memory', 'chroma_db'),
        )

    # ---- 死循环检测：--loop-window 0 表示关掉 ----
    loop_detector = LoopDetector(window=args.loop_window) if args.loop_window else None

    # ---- 预算：只在给了 --max-tokens 时启用（Budget 靠 attach 订阅事件记账）----
    budget = None
    if args.max_tokens:
        budget = Budget(max_tokens=args.max_tokens)
        budget.attach(bus)

    # ---- 审批闸门（P09）：危险操作执行前拦一下 ----
    # ★ 同一个 ApprovalGate，三种行为，只差一个 policy 函数：
    #     ask  → policy=None → input() 问人（真人用）
    #     auto → 永远批准（信得过的批处理）
    #     deny → 永远拒绝（CI / 演练「拦得住吗」）
    from miniagent.runtime.approval import ApprovalGate

    if args.approve == 'auto':
        _policy = lambda name, cmd: True      # noqa: E731
    elif args.approve == 'deny':
        _policy = lambda name, cmd: False     # noqa: E731
    else:
        _policy = None                        # 交互模式
    ApprovalGate(policy=_policy).attach(bus)  # ★ 接入 = 挂个订阅者，主循环一行不用改

    # ---- Checkpoint（P10）：回滚要有个“工作目录”作为边界 ----
    # ★ 它是唯一【不能】靠挂订阅者完成的功能：回滚要把主循环的 step 拨小，
    #   订阅者改不了循环变量，所以 Agent 得直面接一个 ckpt + 一个决策者。
    ckpt = None
    rollback_gate = None
    workdir = os.path.abspath(args.workdir) if args.workdir else None
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        from miniagent.checkpoint import Checkpoint
        from miniagent.runtime.rollback import RollbackGate

        # 喂 mem.message（内部分那个 list 的【引用】）—— 回滚就是原地截断它
        ckpt = Checkpoint(workdir, mem.message)
        if args.rollback == 'ask':
            rollback_gate = RollbackGate()    # 不传 policy → input() 问人
        else:
            rollback_gate = None              # never：装了 ckpt 但不问（只能手摇 rollback）
        print(f'[checkpoint] 工作目录 = {workdir}（启动时 step = {ckpt.step}）')

    # ---- 工具清单：给了 --workdir 就额外挂上文件工具（回滚要有东东可回）----
    tool_specs = [t.strip() for t in args.tools.split(',') if t.strip()]
    if workdir:
        from miniagent.tools import file_tools     # noqa: F401  import 即注册

        # ★ root 必须和 Checkpoint 的 root 一致：越界的文件 Checkpoint 存不了档
        tool_specs += [{'name': 'write_file', 'root': workdir},
                       {'name': 'read_file', 'root': workdir}]

    # ---- 装配 Agent ----
    agent = Agent(
        llm=llm,
        system_message=args.system_message,
        tools=tool_specs,
        max_steps=args.max_steps,
        mem=mem,
        bus=bus,
        rag=rag,
        budget=budget,
        loop_detector=loop_detector,
        max_retries=args.max_retries,
        sleep=time.sleep,
        ckpt=ckpt,                        # ★ P10
        rollback_gate=rollback_gate,      # ★ P10
    )

    # ---- 跑：run() 是生成器，必须迭代 ----
    print(f'用户: {args.question}\n')
    try:
        for msg in agent.run([Message.user(args.question)]):
            if msg.content:
                print(f'{msg.role}> {msg.content}\n')
            elif msg.function_call:
                print(f'assistant> [调工具] {msg.function_call.name}({msg.function_call.arguments})\n')
    except KeyboardInterrupt:
        print('\n[已中断]')
        return 130                                   # 惯例：Ctrl+C 的退出码是 130

    if tracer is not None:
        tracer.close()
        print(f'trace 已写到: {args.trace}')
        print(f'（看它：miniagent replay {args.trace}）')
    return 0


def cmd_replay(args) -> int:
    """回放 trace：直接复用 P05 写好的 replay()，这里只是"接线"。"""
    from miniagent.runtime.tracer import replay

    if not os.path.exists(args.path):
        print(f'找不到 trace 文件: {args.path}', file=sys.stderr)
        return 1
    replay(args.path, trace_id=args.trace_id)
    return 0


def cmd_eval(args) -> int:
    """评测：M6 再接。现在只占位，保证 `eval` 这个子命令是通的。"""
    print('eval 还没实现 —— 计划在 M6 接评测集（届时会跑一批固定问题、比对命中率）。')
    print('现在想验证效果，可以用:')
    print('  python -m miniagent.cli run "问题" --rag miniagent/memory/asn.txt --trace trace.jsonl')
    print('  python -m miniagent.cli replay trace.jsonl')
    return 0


# ============================================================
#  ③ 入口：解析 → 分派 → 退出码
# ============================================================
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)          # ★ 分派：每个子命令自己存了 func
    except KeyboardInterrupt:
        print('\n[已中断]')
        return 130
    except Exception as e:              # 兜底：别把一屏栈甩给用户
        print(f'\n[出错] {type(e).__name__}: {e}', file=sys.stderr)
        print('提示：检查 .env 里的 DASHSCOPE_API_KEY、模型名、以及 --rag 指向的文件是否存在。',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
