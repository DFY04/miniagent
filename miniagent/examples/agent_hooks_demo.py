"""最小可跑版：事件总线 + 工具调用 + 长期记忆（RAG）。

跑法（两种都行）：
    # ① 当模块跑（推荐，路径最干净）
    .venv\\Scripts\\python.exe -m miniagent.examples.agent_hooks_demo

    # ② 直接当脚本跑
    .venv\\Scripts\\python.exe miniagent\\examples\\agent_hooks_demo.py

    # 想换问题就加个参数
    .venv\\Scripts\\python.exe miniagent\\examples\\agent_hooks_demo.py "丁凤阳是哪里人"

【这份文件修掉了原来那几个坑，对照着看】
    1. run() 是生成器：必须 for ... in agent.run(...) 迭代，光调用一行都不执行
    2. subscribe 传的是【函数对象本身】，不能写成 llmstart()（那会立刻调用并订进 None）
    3. subscribe 的键必须是 EventType.XXX 枚举，不是字符串 'llm_start'
    4. 不要自己再 class EventType(Enum)：和 import 进来的不是同一个类，键对不上
    5. handler 要收 **kwargs：事件会带 step / reply / name 等参数过来
    6. bus 全程只创建一次（重建会把前面的订阅全丢掉）
    7. llm 用 miniagent.llm.LLMClient 造，不是把函数名重新赋值成字典
    8. Agent 要传 mem，否则 run() 里 self.mem.add(m) 直接 AttributeError
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv  # noqa: E402

from miniagent.core.hooks import EventBus, EventType  # noqa: E402
from miniagent.core.mem_agent import Agent  # noqa: E402
from miniagent.core.message import Message  # noqa: E402
from miniagent.llm import LLMClient  # noqa: E402
from miniagent.memory.short_term import ShortTermMemory  # noqa: E402
from miniagent.memory.vector_store import DashScopeChromaRAG  # noqa: E402
from miniagent.runtime.tracer import Tracer, replay  # noqa: E402
from miniagent.tools import demo_tools  # noqa: F401,E402  import 一下，now / add 才会被注册

HERE = os.path.dirname(os.path.abspath(__file__))
ASN_TXT = os.path.join(HERE, '..', 'memory', 'asn.txt')
CHROMA_DIR = os.path.join(HERE, '..', 'memory', 'chroma_db')
TRACE_FILE = os.path.join(HERE, 'trace.jsonl')      # ★ P05：事件落盘的地方


# ==================== 订阅者（都是普通函数）====================
# 统一写 **kw：事件负载有 step / reply / name / result / chars 等，收下就行。

def on_llm_start(**kw):
    print(f'  [bus] 开始调用大模型了（第 {kw.get("step")} 步）')


def on_llm_end(**kw):
    print(f'  [bus] 模型回话了：{kw.get("reply")}')


def on_tool_start(**kw):
    print(f'  [bus] 开始执行工具：{kw.get("name")}')


def on_tool_end(**kw):
    print(f'  [bus] 工具返回了：{kw.get("result")}')


def on_rag_end(**kw):
    print(f'  [bus] 长期记忆检索完成，命中 {kw.get("hits")} 条')


def on_run_end(**kw):
    print(f'  [bus] 这轮结束（原因：{kw.get("reason")}）')


def main() -> None:
    load_dotenv()          # 读 .env 里的 DASHSCOPE_API_KEY

    # ---- 1) 建总线（★ 全程只建一次），注册订阅者 ----
    bus = EventBus()
    bus.subscribe(EventType.LLM_START, on_llm_start)      # ★ 传函数名，不带括号
    bus.subscribe(EventType.LLM_END, on_llm_end)
    bus.subscribe(EventType.TOOL_START, on_tool_start)
    bus.subscribe(EventType.TOOL_END, on_tool_end)
    bus.subscribe(EventType.RAG_END, on_rag_end)
    bus.subscribe(EventType.RUN_END, on_run_end)

    # ★ P05：再挂一个“不 print、只写文件”的订阅者
    #   它不是 Agent 的一部分，也不改总线 —— 只是多一个听众。
    tracer = Tracer(TRACE_FILE)
    tracer.attach(bus)

    # ---- 2) 建 LLM 客户端 ----
    llm = LLMClient({'model': 'qwen-max'})

    # ---- 3) 建向量库（长期记忆的底座）----
    rag = DashScopeChromaRAG(
        collection_name='DF_RAG_BASE',
        file_path=ASN_TXT,
        persist_path=CHROMA_DIR,
    )

    # ---- 4) 建 Agent：短期记忆 + 长期记忆 + 总线 ----
    agent = Agent(
        llm=llm,
        system_message='你是一个智能助手。回答时优先依据检索到的参考资料。',
        tools=['now', 'add'],
        max_steps=10,
        mem=ShortTermMemory(max_tokens=8000),      # ★ 不给 mem，run() 第 91 行就崩
        rag=rag,
        bus=bus,
    )

    # ---- 5) 跑 ----（★ 生成器必须迭代，否则一行都不执行）
    #  ★★ 两种循环，别搞混：
    #     内层（Agent 里）：for step in range(max_steps)   一次任务内的"想 → 调工具 → 再想"
    #     外层（这里）：while True                          一场会话里的多轮问答 ← 现在加的就是它
    #  Agent.run() 的契约是"跑一次任务"，不是"守着一场对话"。
    #  会话循环属于调用方，所以写在 main() 里。
    #  注意：mem / agent 都建在 while 外面（只建一次），
    #       否则每轮都是新建的、空记忆，就会"记不住上一轮"。

    # ① 单次模式：命令行带了问题 → 问一句就退（方便脚本化）
    if len(sys.argv) > 1:
        question = sys.argv[1]
        print(f'用户: {question}\n')
        for msg in agent.run([Message.user(question)]):
            print(f'  [yield] {msg.role}: {msg.content}')
        print('\n跑完了。')
        _finish(tracer)
        return

    # ② 交互模式：把「提问 → run → 打印」三步一起包进 while，才能一问再问
    print('输入问题回车提问；exit / quit / q 退出。\n')
    while True:
        try:
            question = input('你: ').strip()
        except (EOFError, KeyboardInterrupt):     # Ctrl+Z / Ctrl+C
            print()
            break
        if not question:                          # 空行跳过
            continue
        if question.lower() in ('exit', 'quit', 'q'):
            break

        print()
        for msg in agent.run([Message.user(question)]):
            # ★ 只传【本轮新消息】。历史已经在 mem 里了，再传一遍就重复了。
            print(f'  [yield] {msg.role}: {msg.content}')
        print()

    _finish(tracer)


def _finish(tracer) -> None:
    """收尾：关掉 trace 文件，再回放一遍看时序。"""
    tracer.close()
    print(f'\n===== 回放 trace（{TRACE_FILE}）=====')
    replay(TRACE_FILE)


if __name__ == '__main__':
    main()
