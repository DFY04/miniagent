"""test_p14_orchestrator.py —— P14 验收测试（全离线、不联网、不烧 API）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p14_orchestrator.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p14_orchestrator

★ 现在的状态：全红（orchestrator 里四个 TODO 都还没写）。这是正常的。

分三组：
    A. 视角渲染 —— ★ 基础零件（_render_view 是纯函数，可以单独测、改一行看一眼）
    B. 主循环   —— 每轮问裁判 + 上下文累积 + 刹车
    C. 参数校验

测试用的假 LLM：
    TaggedLLM   回答里带自己的名字，并记录"我每次看到了哪些消息"（用来断言视角对不对）
    ScriptedLLM 按剧本逐条回答（用来演"裁判"，因为它的输出必须可控）

怎么自己看数据流：
    TaggedLLM.seen 存的是"这个 Agent 每次被调用时收到的完整消息列表"。
    所以断言 llms['批判者'].seen[0] 就是在问："批判者第一次发言时，
    眼里看到的对话长什么样？"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.agent import Agent  # noqa: E402
from miniagent.core.message import FunctionCall, Message  # noqa: E402
from miniagent.multiagent.hub import MultiAgentHub  # noqa: E402
from miniagent.multiagent.orchestrator import Orchestrator  # noqa: E402


# ==================== 假 LLM ====================

class TaggedLLM:
    """回答里带自己的标签；同时把每次收到的 messages 存下来。

    seen[i] = 第 i 次被调用时，这个 Agent "眼里看到的世界"。
    这就是我们检查"视角渲染"对不对的唯一依据。
    """

    def __init__(self, tag: str):
        self.tag = tag
        self.seen: list[list[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        return Message.assistant(f'{self.tag}: 我看完了')


class ScriptedLLM:
    """按剧本逐条回答。剧本用完还问，就直接报错（说明多问了一次裁判）。"""

    def __init__(self, script: list[str]):
        self.script = list(script)
        self.seen: list[list[Message]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError('剧本已用完，但还被问了一次 —— 是不是多问了一次裁判？')
        return Message.assistant(self.script.pop(0))


# ==================== 造测试数据 ====================

def _agent(tag: str) -> tuple[Agent, TaggedLLM]:
    llm = TaggedLLM(tag)
    return Agent(llm=llm), llm


def _cast():
    """三个人：分析师 / 批判者 / 总结者。返回 (agents, 名字->TaggedLLM)"""
    a, la = _agent('分析师')
    b, lb = _agent('批判者')
    c, lc = _agent('总结者')
    return ({'分析师': a, '批判者': b, '总结者': c},
            {'分析师': la, '批判者': lb, '总结者': lc})


def _orch(script: list[str], max_round: int | None = None):
    """造一个编排器。

    script 是"裁判每轮会说什么"。
    max_round 默认 = len(script) —— 刚好把剧本用完，这样测试不会因为
    被测代码"多问了一次裁判"而报 AssertionError（那应该由 ScriptedLLM 抓出来）。
    """
    agents, llms = _cast()
    router_llm = ScriptedLLM(list(script))
    if max_round is None:
        max_round = max(1, len(script))
    orch = Orchestrator(agents, router_agent=Agent(llm=router_llm), max_round=max_round)
    return orch, router_llm, llms


def _only(msgs, role: str) -> list[Message]:
    return [m for m in msgs if m.role == role]


def _names(msgs) -> list[str]:
    return [m.agent_name for m in msgs]


def _roles(msgs) -> list[str]:
    return [m.role for m in msgs]


# ==================== A. 视角渲染（纯函数，先把它单独立到绿） ====================

def test_view_has_no_assistant_when_i_never_spoke():
    """第一次发言的人，视角里不该有任何 assistant 消息。"""
    orch, _, llms = _orch(['批判者'])
    list(orch.run([Message.user('做个登录功能')]))

    view = llms['批判者'].seen[0]
    assert _only(view, 'assistant') == [], \
        f'别人说的话不该以 assistant 身份出现（会让人格串味），实际 {view}'


def test_view_merges_others_into_single_user_message():
    """所有人的话 + 最初的输入，要合并成**一条** user 消息，并带上谁说的。"""
    orch, _, llms = _orch(['批判者', '总结者'])
    list(orch.run([Message.user('做个登录功能')]))

    users = _only(llms['总结者'].seen[0], 'user')
    assert len(users) == 1, f'应该合并成一条 user 消息，实际 {len(users)} 条'

    content = users[0].content
    assert '做个登录功能' in content, f'最初的输入要带上，实际 {content!r}'
    assert '批判者' in content, f'要标明是谁说的，实际 {content!r}'


def test_view_never_writes_literal_None():
    """★ 踩过的坑：原始 user 消息的 agent_name=None，f'{...}' 会写出字符串 'None'。"""
    orch, _, llms = _orch(['批判者'])
    list(orch.run([Message.user('做个登录功能')]))

    content = _only(llms['批判者'].seen[0], 'user')[0].content
    assert 'None' not in content, f'不该出现字面量 None，实际 {content!r}'


def test_view_keeps_own_past_words_as_assistant():
    """★ 同一个人再次发言时，他"上一轮说过的话"必须以 assistant 身份出现。"""
    orch, _, llms = _orch(['分析师', '分析师'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['分析师', '分析师'], f'实际 {_names(out)}'

    view2 = llms['分析师'].seen[1]
    assistants = _only(view2, 'assistant')
    assert len(assistants) == 1, f'自己上一轮的话必须是 assistant，实际 {view2}'
    assert assistants[0].content == out[0].content

    users = _only(view2, 'user')
    assert len(users) == 1
    assert '我看完了' not in users[0].content, '自己的话不该被塞进 user 文本里'


def test_view_preserves_order():
    """自己的话夹在别人中间时，顺序不能乱。

    剧本：批判者 → 分析师 → 批判者
    批判者第 2 次发言时，他的视角应该是：
        [user(别人说的), assistant(我自己上一轮的话), user(别人说的)]
    """
    orch, _, llms = _orch(['批判者', '分析师', '批判者'])
    list(orch.run([Message.user('第一句')]))

    view2 = llms['批判者'].seen[1]
    assert _roles(view2) == ['user', 'assistant', 'user'], \
        f'顺序应该是 [别人的话, 我的话, 别人的话]，实际 {_roles(view2)}'


def test_view_passes_system_message_through():
    """system 消息要原样透传，不能被塞进 user 文本里。"""
    orch, _, llms = _orch(['批判者'])
    list(orch.run([Message.system('回答要简短'), Message.user('做个登录功能')]))

    view = llms['批判者'].seen[0]
    assert view[0].role == 'system' and '回答要简短' in view[0].content, \
        f'system 消息应该原样放在最前面，实际 {view}'
    assert all('回答要简短' not in u.content for u in _only(view, 'user')), \
        'system 消息不该被混进 user 文本'


# ==================== B. 主循环 ====================

def test_runs_every_round_the_router_asks_for():
    """裁判说谁就说谁 —— 顺序由 LLM 每轮决定，不是写死的。"""
    orch, _, _ = _orch(['批判者', '总结者'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['批判者', '总结者'], \
        f'注意不是分析师先上，顺序完全由裁判决定，实际 {_names(out)}'


def test_max_round_one_makes_it_a_one_shot_dispatch():
    """max_round=1 → 退化成"一次性分诊"（也就是原来的 router 模式）。"""
    orch, router_llm, llms = _orch(['批判者'], max_round=1)
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['批判者'], f'实际 {_names(out)}'
    assert len(router_llm.seen) == 1, '只该问一次裁判'
    assert llms['分析师'].seen == [] and llms['总结者'].seen == [], \
        '没被选中的 agent 一次都不该被调用'


def test_views_accumulate_context():
    """★ 上下文累积：第 2 轮的人必须能看到第 1 轮说过的话。"""
    orch, _, llms = _orch(['批判者', '总结者'])
    list(orch.run([Message.user('做个登录功能')]))

    view = llms['总结者'].seen[0]
    users = _only(view, 'user')
    assert len(users) == 1, f'别人的话该合并成一条 user，实际 {len(users)} 条'
    assert '批判者' in users[0].content, \
        f'第 2 轮的人要能看到第 1 轮的发言，实际 {users[0].content!r}'
    assert _only(view, 'assistant') == [], '总结者还没说过话，不该有 assistant'


def test_each_round_only_calls_the_chosen_agent():
    orch, _, llms = _orch(['批判者', '总结者', '分析师'])
    list(orch.run([Message.user('做个登录功能')]))

    assert len(llms['批判者'].seen) == 1, '批判者该被调用 1 次'
    assert len(llms['总结者'].seen) == 1, '总结者该被调用 1 次'
    assert len(llms['分析师'].seen) == 1, '分析师该被调用 1 次'


def test_stops_on_stop_signal():
    """裁判说 '[STOP]' 就停。"""
    orch, router_llm, _ = _orch(['批判者', Orchestrator.STOP])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['批判者'], f'实际 {_names(out)}'
    assert len(router_llm.seen) == 2, '应该问两次裁判（第二次得到 STOP）'


def test_respects_max_round():
    """★ 刹车：跑满 max_round 就停，而且不许再多问一次裁判。"""
    orch, router_llm, llms = _orch(['分析师', '批判者', '总结者'], max_round=2)
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['分析师', '批判者'], f'实际 {_names(out)}'
    assert len(router_llm.seen) == 2, \
        f'跑满 2 轮后不该再多问裁判，实际问了 {len(router_llm.seen)} 次'
    assert llms['总结者'].seen == [], '总结者不该被调用'


def test_router_sees_all_speakers_as_user():
    """★ 裁判拿到的是"上帝视角"：所有人的话都在 user 里，一句 assistant 都没有。"""
    orch, router_llm, _ = _orch(['批判者', '总结者'])
    list(orch.run([Message.user('做个登录功能')]))

    seen = router_llm.seen[1]        # 第 2 轮问裁判时，裁判看到的
    assert _only(seen, 'assistant') == [], \
        f'裁判不该看到 assistant 消息（它不是参与者），实际 {seen}'
    assert any('我看完了' in m.content for m in _only(seen, 'user')), \
        f'裁判应该看到大家的发言，实际 {seen}'


def test_router_prompt_lists_candidates():
    """裁判必须被告知有哪些候选人，否则它没法选。"""
    orch, router_llm, _ = _orch(['批判者'])
    list(orch.run([Message.user('做个登录功能')]))
    asked = ' '.join(m.content for m in router_llm.seen[0])
    for name in ('分析师', '批判者', '总结者'):
        assert name in asked, f'提问里应该列出候选名字 {name}，实际 {asked!r}'


def test_falls_back_to_first_on_unknown_name():
    """裁判胡说八道时不许崩，兜底用第一个。"""
    orch, _, _ = _orch(['我也不知道该给谁'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['分析师'], f'兜底应该选第一个分析师，实际 {_names(out)}'


def test_tolerates_whitespace_and_newline():
    orch, _, _ = _orch(['  批判者\n'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert _names(out) == ['批判者'], f'带空白的 " 批判者\\n" 也要能解析，实际 {_names(out)}'


def test_does_not_yield_router_decision():
    """裁判的问答是内部过程，不该混进输出里。"""
    orch, _, _ = _orch(['批判者', '总结者'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert all(m.agent_name in ('分析师', '批判者', '总结者') for m in out), \
        f'输出里只该有 agent 的话（而且都带 agent_name），实际 {_names(out)}'


def test_does_not_yield_input_messages():
    """只吐"新产出"，不要把调用方传进来的 user 消息再吐一遍。"""
    orch, _, _ = _orch(['批判者'])
    out = list(orch.run([Message.user('做个登录功能')]))
    assert all(m.role != 'user' for m in out), f'user 消息不该出现在输出里，实际 {_roles(out)}'


def test_does_not_mutate_caller_messages():
    orch, _, _ = _orch(['批判者', '总结者'])
    original = [Message.user('做个登录功能')]
    list(orch.run(original))
    assert len(original) == 1, '不该在调用方传进来的 list 上做 extend'


def test_run_is_lazy_until_iterated():
    """run() 必须返回生成器：不迭代就不干活（这是"流式透传"的证据）。"""
    orch, _, llms = _orch(['批判者'])
    gen = orch.run([Message.user('hi')])
    assert all(v.seen == [] for v in llms.values()), 'run() 不该在迭代前就把活干完'
    list(gen)
    assert len(llms['批判者'].seen) == 1


# ==================== D. MultiAgentHub（基类） ====================

def _shared_with_tools():
    """手工造一条共享历史：两个成员，各自调了一次工具。

    真实跑的时候 function 消息的 agent_name 由编排层盖；这里手工补上。
    """
    msgs = [
        Message.user('做个登录功能'),
        Message.assistant(function_call=FunctionCall('sql_query', '{}'), agent_name='分析师'),
        Message.function('sql_query', '[[1,2,3]]'),
        Message.assistant('分析师：需要短信验证码', agent_name='分析师'),
        Message.assistant(function_call=FunctionCall('calc', '{}'), agent_name='批判者'),
        Message.function('calc', '42'),
        Message.assistant('批判者：短信要花钱', agent_name='批判者'),
    ]
    msgs[2].agent_name = '分析师'
    msgs[5].agent_name = '批判者'
    return msgs


def test_hub_rejects_empty():
    try:
        MultiAgentHub({})
        assert False, '空成员应该报错'
    except ValueError:
        pass


def test_hub_rejects_blank_name():
    a, _ = _agent('A')
    for bad in ('', '   '):
        try:
            MultiAgentHub({bad: a})
            assert False, f'空名字 {bad!r} 应该报错'
        except ValueError:
            pass


def test_hub_rejects_reserved_words():
    """★ 保留字必须独占：NOBODY 会污染上帝视角，STOP 会让停止信号失效。"""
    a, _ = _agent('A')
    for reserved in (MultiAgentHub.NOBODY, MultiAgentHub.STOP):
        try:
            MultiAgentHub({reserved: a})
            assert False, f'占用保留字 {reserved!r} 应该报错'
        except ValueError:
            pass


def test_hub_exposes_names_and_contains():
    agents, _ = _cast()
    hub = MultiAgentHub(agents)
    assert hub.agent_names == ['分析师', '批判者', '总结者']
    assert '分析师' in hub and '路人' not in hub
    assert len(hub) == 3


def test_hub_copies_caller_dict():
    """外部改动原始 dict 不该影响 hub。"""
    agents, _ = _cast()
    hub = MultiAgentHub(agents)
    agents['路人'] = agents['分析师']
    assert '路人' not in hub, 'hub 应该自己存一份，不该跟调用方共用 dict'


def test_render_view_keeps_my_own_tool_messages():
    """★ 我自己的工具过程（function 消息）必须原样保留。"""
    view = MultiAgentHub._render_view('分析师', _shared_with_tools())

    assert _roles(view) == ['user', 'assistant', 'function', 'assistant', 'user'], \
        f'顺序和角色应该都保住，实际 {_roles(view)}'
    assert any('[[1,2,3]]' in m.content for m in view), '自己的工具结果要留下来'


def test_render_view_keeps_position_when_i_speak_twice():
    """★★ 自己的话穿插在别人中间 —— 位置不能变，别人的话要按段分开。

    原时间线：用户 → 我(A1) → 别人(B1) → 我(A2) → 别人(B2)
    我的视角：user(用户) → A1 → user(B1) → A2 → user(B2)

    绝不能写成“别人的话全堆前面、我的全堆后面”——那会变成
    “别人先说完，我再总结”，因果顺序就反了。
    """
    shared = [
        Message.user('做个登录功能'),
        Message.assistant('A1：先看需求', agent_name='分析师'),
        Message.assistant('B1：短信要钱', agent_name='批判者'),
        Message.assistant('A2：那就改用邮箱', agent_name='分析师'),
        Message.assistant('B2：邮箱也有成本', agent_name='批判者'),
    ]
    view = MultiAgentHub._render_view('分析师', shared)

    assert _roles(view) == ['user', 'assistant', 'user', 'assistant', 'user'], \
        f'实际 {_roles(view)}'

    assert [m.content for m in _only(view, 'user')] == [
        'user: 做个登录功能',
        '批判者: B1：短信要钱',
        '批判者: B2：邮箱也有成本',
    ], '每一段连续的别人的话各自压成一条 user，共 3 条'


def test_render_view_merges_only_adjacent_others():
    """换个人看：连着两段别人的话应该合成一条，不连着的不合。"""
    shared = [
        Message.user('做个登录功能'),
        Message.assistant('A1：先看需求', agent_name='分析师'),
        Message.assistant('B1：短信要钱', agent_name='批判者'),
        Message.assistant('A2：那就改用邮箱', agent_name='分析师'),
    ]
    view = MultiAgentHub._render_view('批判者', shared)

    assert _roles(view) == ['user', 'assistant', 'user'], f'实际 {_roles(view)}'
    assert _only(view, 'user')[0].content == 'user: 做个登录功能\n分析师: A1：先看需求', \
        '用户那段和分析师那段是连着的，合成一条'
    assert _only(view, 'user')[1].content == '分析师: A2：那就改用邮箱'


def test_render_view_drops_others_tool_messages():
    """★ 别人的工具过程是噪音，要丢掉（只留结论）。"""
    view = MultiAgentHub._render_view('分析师', _shared_with_tools())

    assert not any('42' in (m.content or '') for m in view), \
        '批判者调工具拿到的原始结果不该出现在分析师的视角里'
    merged = _only(view, 'user')[-1].content
    assert '批判者：短信要花钱' in merged, '但结论要留下'


def test_render_view_skips_empty_others_messages():
    """别人"请求调工具"那条 assistant，content 是空的，不该留下一行裸名字。"""
    view = MultiAgentHub._render_view('分析师', _shared_with_tools())
    lines = _only(view, 'user')[-1].content.split('\n')
    assert all(line.strip() not in ('分析师:', '批判者:') for line in lines), \
        f'不该有光秃秃的名字行，实际 {lines}'


def test_render_view_does_not_mutate_shared():
    """渲染不许改动共享历史本身（它是唯一的真相，只读）。"""
    shared = _shared_with_tools()
    snapshot = [(m.role, m.agent_name, m.content) for m in shared]
    MultiAgentHub._render_view('分析师', shared)
    assert [(m.role, m.agent_name, m.content) for m in shared] == snapshot, \
        '不该修改共享历史里的任何消息'
    assert len(shared) == 7, '不该往共享历史里追加东西'


# ==================== E. 参数校验 ====================

def test_empty_agents_raises():
    r = Agent(llm=ScriptedLLM(['a']))
    try:
        Orchestrator({}, router_agent=r)
        assert False, '空 agents 应该报错'
    except ValueError:
        pass


def test_max_round_must_be_positive():
    agents, _ = _cast()
    r = Agent(llm=ScriptedLLM(['a']))
    try:
        Orchestrator(agents, router_agent=r, max_round=0)
        assert False, 'max_round=0 应该报错（那就一轮都不跑了）'
    except ValueError:
        pass


def test_requires_router_agent():
    agents, _ = _cast()
    try:
        Orchestrator(agents, router_agent=None)
        assert False, '必须要求 router_agent'
    except ValueError:
        pass


# ==================== 脚本模式入口 ====================

def main() -> None:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  ✅ {name}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {name}  ->  {type(e).__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
