"""P09 —— 审批闸门（approval）：危险操作【执行前】停一下，等人批准。

拒绝 → 抛 ApprovalRejected → 主循环收到 → RUN_END(reason='rejected') + 优雅结束。

【三个设计要点（都是踩过坑的）】

① 它就是一个【订阅者】（跟 Tracer / Budget 一样）。
     接入方式 = gate.attach(bus) → bus.subscribe(EventType.TOOL_START, self.on_tool_start)
   ★ 主循环里那句 `except ApprovalRejected` 早就写好了（core/mem_agent.py:188），
     所以"接入审批"根本不需要改主循环 —— 这就是事件总线架构的回报。

② ApprovalRejected 必须继承 ControlFlowSignal（core/hooks.py 里已经是了），
   否则它会在总线的 `except Exception` 里被"已隔离"吞掉 → 审批静默失效。

③ 决策来源必须【可注入】（policy 参数）：
     真人用 → 不传 policy → 走 input() 问人
     测试/CI → 传 policy=lambda name, args: False → 立刻拒绝，不等人敲键盘（"不阻塞"）

【用法】
    gate = ApprovalGate(dangerous=['DROP', 'DELETE', 'rm', 'sudo'])   # 或用默认表
    gate.attach(bus)

    # 测试里（不阻塞）：
    gate = ApprovalGate(policy=lambda name, args: False)   # 永远拒绝
"""
from __future__ import annotations

from typing import Callable, Optional

from miniagent.core.hooks import ApprovalRejected, EventType


class ApprovalGate:
    """危险工具执行前的审批闸门。"""

    # 默认危险词（大小写不敏感）：
    #   SQL 的破坏性语句 + 危险 shell 命令
    DEFAULT_DANGEROUS = ('DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'TRUNCATE',
                         'RM ', 'SUDO', 'SHUTDOWN', 'FORMAT')

    def __init__(self,
                 dangerous: Optional[list] = None,
                 policy: Optional[Callable[[str, str], bool]] = None):
        """
        Args:
            dangerous: 危险词表。不传就用 DEFAULT_DANGEROUS。
            policy:    决策函数 `policy(tool_name, args) -> bool`（True = 批准）。
                       不传 → 交互模式（input 问人）；
                       传了 → 听它的（测试 / CI / 无人值守，不阻塞）。
        """
        self.dangerous = tuple(dangerous) if dangerous else self.DEFAULT_DANGEROUS
        self.policy = policy

    # ---------------- 挂到总线上 ----------------
    def attach(self, bus) -> None:
        """订阅 TOOL_START 事件。

        ★ 注意 subscribe 是【两个位置参数】：(事件类型, 处理函数)。
          别写成 bus.subscribe(self.on_tool_start)（少参数）或
          bus.subscribe(policy, event_type=...)（凭空多个关键字）。
        """
        bus.subscribe(EventType.TOOL_START, self.on_tool_start)

    # ---------------- 订阅者本体 ----------------
    def on_tool_start(self, **kw) -> None:
        """TOOL_START 事件一到就检查：危险 → 问批准 → 拒绝就 raise。

        ★ 签名只有 **kw —— 因为这是 `subscribe`（单事件订阅），
          负载里没有 event，事件类型在注册时就说明了（见 PYTHON_NOTES §5.7）。
        """
        name = kw.get('name', '')
        args = kw.get('args', '') or ''

        # 注意：如果 TOOL_START 的负载里没有 args（老版本埋点），这里会判不出危险 →
        # 一律放行。所以 core/mem_agent.py 那行 _emit 必须带上 args=...（已修）。
        if not self.is_dangerous(args):
            return                          # ← 通过：什么都不做（不返回值）

        if self._ask(name, args):
            return                          # ← 批准：同样什么都不做
        raise ApprovalRejected(name)        # ← 拒绝：抛出去，会穿过总线到主循环

    # ---------------- 内部：判断 + 决策 ----------------
    def is_dangerous(self, args: str) -> bool:
        """检查【参数内容】里有没有危险词。

        ★ 危险的是参数（`DROP TABLE users`），不是工具名（`sql_query`）——
          拿工具名去比危险词表是语义错误。
        """
        upper = (args or '').upper()
        return any(word.upper() in upper for word in self.dangerous)

    def _ask(self, name: str, args: str) -> bool:
        """谁来决策：注入的 policy 优先；没注入才问人。"""
        if self.policy is not None:
            return bool(self.policy(name, args))          # ★ 不阻塞

        print('\n⚠️  模型要执行危险操作：')
        print(f'     工具: {name}')
        print(f'     参数: {args}')
        answer = input('   批准吗？(y/n) ')                # 只有真人交互时才走到这里
        return answer.strip().lower() == 'y'


if __name__ == '__main__':
    # 自测：不联网、不用总线，直接喂负载看行为
    from miniagent.core.hooks import EventBus

    print('=== ① 危险操作 + 拒绝（注入 policy，不阻塞）===')
    gate = ApprovalGate(policy=lambda name, args: False)
    try:
        gate.on_tool_start(name='sql_query', args='DROP TABLE users')
        print('   ❌ 没拦住')
    except ApprovalRejected as e:
        print('   ✅ 拦住了:', e)

    print('\n=== ② 安全操作 → 放行 ===')
    gate.on_tool_start(name='sql_query', args='SELECT * FROM users LIMIT 10')
    print('   ✅ 放行了')

    print('\n=== ③ 大小写不敏感 ===')
    try:
        gate.on_tool_start(name='sql_query', args='drop table users')
        print('   ❌ 没拦住')
    except ApprovalRejected:
        print('   ✅ 小写 drop 也拦住了')

    print('\n=== ④ 挂到总线上，异常能穿出来 ===')
    bus = EventBus()
    ApprovalGate(policy=lambda name, args: False).attach(bus)
    try:
        bus.emit(EventType.TOOL_START, name='sql_query', args='DELETE FROM users')
        print('   ❌ 异常被总线吞了')
    except ApprovalRejected as e:
        print('   ✅ 穿过总线了:', e)
