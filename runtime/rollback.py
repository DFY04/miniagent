"""P10 —— 回滚闸门（rollback gate）：每步结束时决定「要不要回到从前」。

【它和审批闸门（P09 ApprovalGate）长得很像，但有一个本质区别】

    ApprovalGate 是【纯订阅者】：它只决定"放行 / 拦截"，拦下来抛个异常就完事，
    主循环一行都不用改（靠事件总线 + ControlFlowSignal）。

    RollbackGate 做不了纯订阅者 —— 因为回滚要改的是【循环变量】：
    回滚之后主循环的 step 必须变小、下一轮得从小的那个数重跑。
    订阅者在事件总线里只能拿到事件负载，既看不到也改不了主循环的 step。
    所以它只能由主循环【直接调用】（core/mem_agent.py 的 STEP_END 之后）。

    ★ 判断标准：能订阅着解决的用订阅；订阅解决不了的，才动主循环。
      这比"一律挂事件总线"要多想一步，但改动更小、时序更清楚。

【决策来源必须可注入（和 ApprovalGate 完全一样的套路）】

    真人用   → 不传 policy → input() 问人
    测试/CI  → 传 policy=lambda step: 0 → 立刻返回，不等人敲键盘（不阻塞）

    policy 的约定：
        policy(step) -> Optional[int]
            返回 None          = 不回滚，继续往下跑
            返回整数 target    = 回滚到 target 之前（会被夹进 [0, step]）

    ★ 为什么用 None 表示"不回滚"、而不是用 -1 或 0：
      0 是一个【合法】的回滚目标（回滚到最开始，全部撤销）。
      如果拿 0 当"不回滚"的哨兵值，就永远回滚不到开头了。
      同理，判断时必须写 `target is None` 而不是 `if not target` ——
      后者会把 0 当成假值放过去，回滚请求被静默吞掉。
"""
from __future__ import annotations

from typing import Callable, Optional


# ============================================================
#  两个纯函数：字符串 → 目标步号、目标步号 → 合法步号
#  （抽出来单独放，是为了能脱离 Agent 单独测试）
# ============================================================

def parse_target(raw: str, current_step: int) -> Optional[int]:
    """把用户敲的一行字符串，翻译成"回滚到第几步之前"。

    Args:
        raw: 用户输入（input() 拿到的东西，永远是 str）。
        current_step: 当前步号，用于兜底。

    Returns:
        None  -> 用户没给有效步号（空行 / 乱敲）→ 由调用方决定默认动作
        0..N  -> 解析出来的整数

    为什么要 .strip()：input() 会把行尾的 \\n 去掉，但用户手敲的前后空格不会，
    所以 ' 2 ' 必须能被认出来。为什么要 isdigit() 挡在 int() 前面：
    int('') 和 int('abc') 会抛 ValueError 直接打断程序。
    """
    raw = (raw or '').strip()
    if not raw.isdigit():          # isdigit(): 这一串是不是【全由数字字符组成】
        return None
    return int(raw)                # int(): str -> int，这一步才是真正的"类型转换"


def clamp_target(target: int, current_step: int) -> int:
    """把目标步号夹进合法区间 [0, current_step]。

    为什么要夹，而不是直接报错：
        Checkpoint.rollback 只在 target > self.step（滚到还没走过的未来）时抛
        ValueError。与其让用户敲错一个数就把 agent 打断，不如就地夹住：
            敲了 99（还没有的未来）→ 夹成 current_step（= 撤销最近这一步）
            敲了 -3（负数）        → 夹成 0（全部撤销）

    min 取小的、max 取大的，两层套起来就是"夹范围"（clamp）。
    """
    return max(0, min(int(target), current_step))


# ============================================================
#  闸门本体
# ============================================================

class RollbackGate:
    """每步结束问一句「要不要回滚」，把用户的意图翻译成一个合法步号。

    用法（真人交互）：
        agent = Agent(llm=llm, mem=mem, ckpt=ckpt, rollback_gate=RollbackGate())

    用法（测试 / 无人值守，不阻塞）：
        gate = RollbackGate(policy=lambda step: 0)    # 每步结束都回滚到开头
        gate = RollbackGate(policy=lambda step: None) # 永远不回滚（等于关掉）
    """

    def __init__(
        self,
        policy: Optional[Callable[[int], Optional[int]]] = None,
        ask_prompt: str = '是否回滚?(y/n) ',
        step_prompt: str = '回滚到第几步之前? ',
    ):
        """
        Args:
            policy: 决策函数 `policy(step) -> Optional[int]`。
                    不传 → 交互模式（input 问人）；
                    传了 → 听它的（测试 / CI / 无人值守，不阻塞）。
            ask_prompt: 交互模式下"确认要不要回滚"的提示语。
            step_prompt: 交互模式下"回滚到第几步"的提示语。
        """
        self.policy = policy if policy is not None else self._ask
        self.ask_prompt = ask_prompt
        self.step_prompt = step_prompt

    # ---------------- 默认策略：问真人 ----------------
    def _ask(self, step: int) -> Optional[int]:
        """交互模式：先问"要不要"，再问"回到哪一步"。

        ★ 这是唯一会碰 input() 的地方。测试里只要注入 policy，
          就绝不会走到这两行 —— 这就是"不阻塞"的字面含义。
        """
        if input(self.ask_prompt).strip().lower() != 'y':
            return None                                   # 不回了，继续跑

        raw = input(self.step_prompt)
        target = parse_target(raw, step)
        if target is None:
            # 直接回车 / 敲了非数字 → 默认"撤销最近这一步"（最常见的心智模型：
            # "刚才那步不对，再来一次"）
            return step
        return target

    # ---------------- 主循环唯一要调的方法 ----------------
    def decide(self, step: int) -> Optional[int]:
        """问一次决策，返回【已经夹好范围】的目标步号；None = 不回滚。"""
        target = self.policy(step)
        if target is None:              # ★ 必须用 is None，不能用 if not target
            return None
        return clamp_target(target, step)
