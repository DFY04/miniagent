"""多智能体的公共基类：**存人 + 身份转换**。

════════════════════════════════════════════════════════════════════
【为什么需要这个基类】

    多智能体系统里有两类完全不同的事：

        ① 群级能力   —— 任何多智能体玩法都要用的，跟"怎么编排"无关
                        例：成员管理、把共享上下文翻译成某个成员的视角
        ② 编排策略   —— 谁先上、跑几轮、什么时候停
                        例：LLM 每轮选人（Orchestrator）

    这两类东西混在一个类里，会导致换个编排方式就得重写一遍身份转换。
    所以把 ① 提到基类，② 留给子类。

    （对应 qwen-agent 的 qwen_agent/multi_agent_hub.py。区别是 qwen-agent 的
      MultiAgentHub 只做成员管理，身份转换留在 GroupChat 里；这里把它提上来，
      因为它才是最容易写错、最值得复用的那部分。）

════════════════════════════════════════════════════════════════════
【★ 身份转换：多智能体最核心的一段代码 ★】

    背景：共享历史（shared）是一条**混着所有人发言的时间线**：

        shared = [
            Message(role='user',      agent_name=None,     content='做个登录功能'),
            Message(role='assistant', agent_name='分析师',  content='需要短信验证码'),
            Message(role='assistant', agent_name='批判者',  content='短信要花钱'),
        ]

    **不能**把这条线直接丢给某个 Agent。因为对话模型的世界观里只有两种角色：

        assistant = "我自己说过的话"
        user      = "外面给我的输入"

    批判者看到那两条 assistant，会认为"这些是我说的"，于是接着自己的话往下编 ——
    人格串味，整个流程就毁了。

    所以必须针对"谁在看"现场翻译一遍：

        _render_view('批判者', shared)  ==>
            [ Message(role='user', content='user: 做个登录功能\\n分析师: 需要短信验证码') ]

        _render_view('分析师', shared)  ==>
            [ Message(role='user',      content='user: 做个登录功能'),
              Message(role='assistant', agent_name='分析师', content='需要短信验证码'),
              Message(role='user',      content='批判者: 短信要花钱') ]

    规则四条：

        system 消息                      → 原样透传（放最前面）
        m.agent_name == me（自己的话）    → 原样保留（assistant 还是 assistant）
        m.agent_name == me 的 function   → 也原样保留（我自己的工具过程）
        每一段【连续的别人的话】           → 压成【一条 user 消息】
                                          （被自己的话切成几段，就有几条 user）
        别人的 function / 空消息          → 丢掉（噪音）

    ★★ 最关键的一点：不是“别人的话全合并成一条”，
       而是“按自己的发言位置**分段**合并，相对位置一根不能变”

        自己的话穿插在别人中间时，之前一段、之间一段、之后一段
        各自压成一条 user：

        shared（原时间线）:
            用户  : 做个登录功能
            分析师: A1：先看需求          ← 我
            批判者: B1：短信要钱
            分析师: A2：那就改用邮箱      ← 我
            批判者: B2：邮箱也有成本

        _render_view('分析师', shared) ==>          ← 我眼里
            user      'user: 做个登录功能'            ┐
            assistant 'A1：先看需求'                  │ 我之前的一段
            user      '批判者: B1：短信要钱'          ┘
            assistant 'A2：那就改用邮箱'              ← 我夹在中间
            user      '批判者: B2：邮箱也有成本'      ← 我之后的一段

        _render_view('批判者', shared) ==>          ← 换成批判者的视角
            user      'user: 做个登录功能\n分析师: A1：先看需求'   ← 连着两段合成一条
            assistant 'B1：短信要钱'
            user      '分析师: A2：那就改用邮箱'
            assistant 'B2：邮箱也有成本'

        ★ 如果错误地把“别人的话全堆到前面、自己的话全堆到后面”，
          就变成了“别人先说完，我再总结”—— 因果顺序彻底乱了。

    ★ “攒 + flush”就是为实现“分段”而生的：
      遇到自己的话就把篮子倒一次，所以篮子里的东西天然就是
      “从上一个自己的话到这一个自己的话之间的所有别人的话”——
      一段一段自动分开，不用手工切。

    ★ 同一个人第二次、第三次发言怎么办？
      不用任何特殊处理。判断依据是 `m.agent_name == me`：
      他以前说的那些话 agent_name 也是他，所以**天然就是 assistant**；
      而别人的话不管出现多少次，都自然被分段包进 user 里。
      “身份转换”顺手就解决了“重复发言”问题。

════════════════════════════════════════════════════════════════════
【★ 一个函数两种用途：上帝视角 ★】

    给 _render_view 传一个**绝不会等于任何成员名字的哨兵名**（self.NOBODY）：

        _render_view('__nobody__', shared)  ==>
            [ Message(role='user', content='user: 做个登录功能\\n分析师: ...\\n批判者: ...') ]

    因为 `m.agent_name == '__nobody__'` 对所有消息都是 False，
    所有人的话都被当成"别人的话"，**全部合并成一条 user** ——
    这正是"裁判"需要的视角：它不是参与者，它只是读者。

    （qwen-agent 的 GroupChatAutoRouter 把历史压成 dialogue 列表，同一个思路。）
"""
from __future__ import annotations

from abc import ABC
from typing import Dict, List, Mapping, Sequence

from miniagent.core.agent import Agent
from miniagent.core.message import Message


class MultiAgentHub(ABC):
    """一群 Agent 的公共基类。

        ┌─────────────────────────────────────────────┐
        │ MultiAgentHub      成员管理 + 身份转换        │ ← 本文件
        ├─────────────────────────────────────────────┤
        │ Orchestrator       顺序策略：LLM 每轮选人      │ ← orchestrator.py
        └─────────────────────────────────────────────┘

    成员用 `名字 -> Agent` 的字典存放。**名字就是成员的身份证**，
    后面所有判断（"这句是不是我说的"、"下一个该谁"）都靠它。
    """

    # ★ 哨兵名：一个绝不会等于任何成员名字的特殊名字。
    #   传给 _render_view 当 me 用 → 得到"上帝视角"。
    NOBODY = '__nobody__'

    # ★ 停止信号：裁判输出它就表示"话题结束，别再选人了"。
    STOP = '[STOP]'

    def __init__(self, agents: Mapping[str, Agent]):
        if not agents:
            raise ValueError('agents 不能为空：没有成员的多智能体编排没有意义')

        agents = dict(agents)          # 复制一份，别跟调用方共用同一个 dict
        reserved = (self.NOBODY, self.STOP)
        for name, agent in agents.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f'成员名字必须是非空字符串，收到 {name!r}')
            if name in reserved:
                # ★ 保留字必须独占：
                #   叫 NOBODY 会把"上帝视角"污染成"那个人的视角"；
                #   叫 STOP 则会让停止信号永远无法生效（成员名检查在 STOP 之前）。
                raise ValueError(f'{name!r} 是保留字，不能拿来当成员名字（保留：{list(reserved)}）')
            if agent is None:
                raise ValueError(f'成员 {name!r} 是 None')

        self._agents: Dict[str, Agent] = agents

    # ================= 成员管理（只读视图） =================

    @property
    def agents(self) -> Dict[str, Agent]:
        """名字 -> Agent。dict 的 key 天然唯一，所以重名这条防线是白送的。"""
        return self._agents

    @property
    def agent_names(self) -> List[str]:
        return list(self._agents)

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __repr__(self) -> str:
        return f'{type(self).__name__}(agents={self.agent_names})'

    # ================= ★ 身份转换 =================

    @staticmethod
    def _render_view(me: str, shared: Sequence[Message]) -> List[Message]:
        """把"共享历史"渲染成 `me` 这个成员的视角。

        参数：
            me      —— 谁在看？传成员名字 → 成员视角；
                       传 NOBODY 哨兵名 → 上帝视角（裁判用）
            shared  —— 共享历史（唯一的真相），每条产出都带 agent_name

        返回：
            一个 List[Message]，就是 me 眼里"这次对话长什么样"。

        规则见模块 docstring。核心技巧只有一个 —— **攒 + flush**：

            开一个 pending 篮子攒“别人的话”；
            遇到“自己的话”就先 flush（把篮子里的拼成一条 user 消息交出去），
            再放自己的消息；循环结束再 flush 一次收尾。

        ★ 为什么必须“攒”而不能“来一条建一条”？
            因为要多条**连续**的话压成一条 user —— 模型会把连续多条 user
            当成“用户连说了好几次”，压成一条 + 每行 `说话人: 内容`，
            才是它读得懂的“会议纪要”。

        ★ 为什么是“遇到自己的话就 flush”，而不是“攒到最后一次性输出”？
            因为**位置不能变**。自己的话可能出现在中间，那它前面和后面
            的别人的话应该拆成**两条** user，而不是拼成一条。
            pending 篮子天然就按“自己的话”把历史切成了若干段 ——
            这就是分段合并的实现方式。
        """
        view: List[Message] = []
        pending: List[str] = []

        def flush() -> None:
            """把篮子里攒的别人的话，打包成一条 user 消息交出去。"""
            if pending:
                view.append(Message.user('\n'.join(pending)))
                pending.clear()

        for m in shared:
            # ① system：不是任何人的发言，原样透传
            if m.role == 'system':
                view.append(m)
                continue

            # ② 我自己的话（含我自己的工具过程）：原样保留
            if m.agent_name == me:
                flush()                 # 交出去之前，先把攒的别人的话倒掉
                view.append(m)
                continue

            # ③ 别人的工具消息（原始 JSON）：纯噪音，丢掉
            if m.role == 'function':
                continue

            # ④ 别人的空消息（比如"请求调工具"那条 assistant，content 是空的）：
            #    留着只会得到一行光秃秃的 `某人: `，也丢掉
            if not m.content:
                continue

            # ⑤ 别人的话：攒着，标上是谁说的
            #    ★ 坑：agent_name 可能是 None（调用方给的 user 消息），
            #      f'{None}' 会写出字符串 'None'，所以用 `or m.role` 兜底
            label = m.agent_name or m.role
            pending.append(f'{label}: {m.content}')

        flush()                         # 收尾：最后一批别人的话也要交出去
        return view
