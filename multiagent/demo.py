"""P14 · Orchestrator 离线演示 —— 不需要 API key、不联网，直接跑。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.multiagent.demo

它做三件事：
    1. 用假 LLM 搭一个三人小组：分析师 / 批判者 / 总结者
    2. 用一份"裁判剧本"控制每轮谁发言（真跑时裁判是 LLM，这里为了可复现写成死的）
    3. 把每一步的中间数据都打印出来 —— 特别是"身份转换"那一步

看的时候盯住两件事：
    ★ 裁判看到的永远是"一条 user"（上帝视角：他不是参与者，只是读者）
    ★ 分析师第二次发言时，他自己的话变成了 assistant，别人的话停在 user 里
"""
from __future__ import annotations

from typing import Dict, List

from miniagent.core.agent import Agent
from miniagent.core.message import Message
from miniagent.multiagent.hub import MultiAgentHub
from miniagent.multiagent.orchestrator import Orchestrator


# ==================== 假 LLM ====================

class ScriptedLLM:
    """按剧本逐条回答 —— 演"裁判"。"""

    def __init__(self, script: List[str]):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if not self.script:
            raise AssertionError('剧本用完了，但还被问了一次 —— 是不是多问了一次裁判？')
        return Message.assistant(self.script.pop(0))


class FixedLLM:
    """每次都说同一句话 —— 演"干活的成员"。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.seen: List[List[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        return Message.assistant(self.reply)


# ==================== 打印工具 ====================

def dump(msgs, indent: int = 4, mark_own: str | None = None) -> None:
    pad = ' ' * indent
    if not msgs:
        print(f'{pad}(空)')
        return
    for m in msgs:
        who = m.agent_name or '—'
        star = ''
        if mark_own and m.agent_name == mark_own:
            star = '  ★ 我自己的话'
        print(f'{pad}{m.role:<10} [{who:<5}]{star}')
        for line in (m.content or '').split('\n'):
            print(f'{pad}    {line}')


def hr(title: str = '', ch: str = '─', width: int = 72) -> None:
    if title:
        print(f'\n{ch * 3} {title} {ch * max(0, width - len(title) - 6)}')
    else:
        print(ch * width)


# ==================== 主流程 ====================

SCRIPT = ['分析师', '批判者', '分析师', Orchestrator.STOP]

REPLIES = {
    '分析师': '登录方式建议：手机号 + 验证码，最省事。',
    '批判者': '验证码有短信成本，而且海外用户根本收不到。',
    '总结者': '结论：先做手机号 + 验证码，同时把邮箱登录的接口预留出来。',
}


def build() -> tuple[Orchestrator, Dict[str, FixedLLM], ScriptedLLM]:
    """搭三个人 + 一个裁判。"""
    members = {}
    llms: Dict[str, FixedLLM] = {}
    agents: Dict[str, Agent] = {}
    for name, reply in REPLIES.items():
        llm = FixedLLM(reply)
        members[name] = llm
        agents[name] = Agent(llm=llm)

    router_llm = ScriptedLLM(SCRIPT)
    orch = Orchestrator(agents, router_agent=Agent(llm=router_llm), max_round=4)
    return orch, members, router_llm


def main() -> None:
    print('=' * 72)
    print('P14 · Orchestrator 离线演示（GroupChat auto 模式）')
    print('=' * 72)
    print(f'成员：{list(REPLIES)}')
    print(f"裁判剧本：{SCRIPT}（'[STOP]' 表示话题结束）")
    print('max_round = 4')

    orch, member_llms, router_llm = build()

    # ---- 手动重现 _run 的循环，好把每一步都打出来 ----
    shared: List[Message] = [Message.user('我想做一个登录功能')]
    yielded: List[Message] = []

    hr('初始 shared（调用方给的输入）')
    dump(shared)

    for rnd in range(1, orch.max_round + 1):
        hr(f'第 {rnd} 轮', '=', 72)

        # ① 问裁判
        router_view = orch._render_view(orch.NOBODY, shared)
        print('  ① 裁判看到（上帝视角：所有人的话都在一条 user 里）')
        dump(router_view, 6)

        answer = router_llm.chat(router_view + [Message.user('下一个是谁？')]).content
        print(f'  ② 裁判回答 → {answer!r}')

        if answer == orch.STOP:
            print('  → 裁判说话题结束，循环退出')
            break

        # ② 渲染视角
        view = orch._render_view(answer, shared)
        print(f'  ③ {answer} 眼里的对话（★ 身份转换的结果）')
        dump(view, 6, mark_own=answer)

        # ③ 干活
        produced, _final = orch._ask(orch.agents[answer], view)
        print(f'  ④ {answer} 产出   ← 注意 agent_name 还是空的：Agent 不知道自己在群里的名字')
        dump(produced, 6)

        # ④ 盖章 + 写回账本
        for m in produced:
            m.agent_name = answer
        print(f'     （编排层盖上 agent_name={answer!r}）')
        yielded.extend(produced)
        shared.extend(produced)

        print('  ⑤ shared（账本）变成')
        dump(shared, 6)

    hr('最终 yield 给调用方的东西')
    dump(yielded)

    hr('核对')
    print(f'  裁判被问了 {router_llm.calls} 次')
    print(f'  每个人被调用的次数：{ {k: len(v.seen) for k, v in member_llms.items()} }')
    print(f'  共享历史 {len(shared)} 条，产出 {len(yielded)} 条')
    print('  ★ 注意：分析师被调用了 2 次，他第 2 次看到的自己是 assistant，'
          '别人是 user')
    print('  ★ 注意：裁判的提问/回答没有出现在最终产出里，也没进共享历史')


if __name__ == '__main__':
    main()
