"""P07 —— 重试（指数退避）+ 死循环检测（止损）。

两个功能加起来只有 20 行左右的真代码，别被术语吓到：

    重试：失败了别立刻再试（对端正在限流，你只会加重拥堵），
          等 1s → 2s → 4s… 再试，并且每次等待都加一点随机抖动（jitter），
          避免多个 worker 在同一时刻一起重试（惊群）。
    止损：同一个工具、同一组参数，连着来 3 次 —— 说明模型卡住了，打断它。

跑一遍看效果：
    .venv\\Scripts\\python.exe -m miniagent.runtime.retry
"""
from __future__ import annotations

import hashlib
import random
import time
from collections import deque
from typing import Callable, Optional, Type

from miniagent.core.hooks import ControlFlowSignal


# ============================================================
#  异常
# ============================================================
class LoopDetected(ControlFlowSignal):
    """同一个工具 + 同一组参数，连着来了太多次。

    ★ 为什么继承 ControlFlowSignal（而不是 Exception）：
      它要被主循环 except 到、用来【中断任务】。
      而 EventBus.emit 对普通异常是"隔离掉、主流程继续"——不继承的话，
      止损会在总线上被静默吞掉（和 BudgetExceeded 同一个坑）。
    """

    def __init__(self, tool_name: str = '', args: str = '', times: int = 0):
        super().__init__(f'检测到死循环：`{tool_name}` 连续 {times} 次用相同参数调用')
        self.tool_name = tool_name
        # ★ 千万别写 self.args = args —— args 是 BaseException 的内置属性（存消息用的），
        #   它的 setter 会把赋值转成元组：字符串 '{}' 会变成 ('{', '}')，消息被挤掉。
        #   实测：str(e) 会变成 "('{', '}')"。所以自己另起个名字。
        self.tool_args = args
        self.times = times


# ============================================================
#  重试：指数退避
# ============================================================
def with_retry(
    fn: Callable[[], object],
    max_retries: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    retry_on: Type[BaseException] = Exception,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    jitter: Callable[[], float] = random.random,
) -> object:
    """执行 fn()；失败就退避后重试；用尽仍失败 → 把最后一个异常原样抛出。

    Args:
        fn: **零参数**可调用。要带参数就自己包一层：
            `with_retry(lambda: llm.chat(history, tools=tools))`
            为什么要求零参数？这样 with_retry 不用管"被调用的东西有几种参数形状"。
        max_retries: 最多重试几次（不含第一次）。max_retries=3 → 最多调用 4 次。
        base_delay: 退避基数。第 n 次重试前等 base_delay * 2**n（n 从 0 数）。
        sleep: 睡眠函数。**测试必须注入一个空函数**（`sleep=lambda s: None`），
               否则跑一次测试要真等 1+2+4 = 7 秒。
        retry_on: 只重试这个类型的异常。其他异常立刻往上抛 ——
                  参数写错这种错误，重试 100 次也没用。
        on_retry: 可选回调 (第几次重试, 要等多久, 异常)。接日志/事件总线用。
        jitter: 抖动函数，返回 [0, 1) 的随机数。实际等待 = 退避时间 * (1 + jitter())，
                即落在 [退避时间, 2 × 退避时间)。
                ★ 为什么要有：多个客户端同时失败时，不加抖动就会【同一时刻】一起重试
                  （惊群），把刚恢复的服务又打垮一次。
                ★ 可测试性：默认 random.random；测试注入 `jitter=lambda: 0.0`
                  就得到确定的 1/2/4 序列，注入 `random.Random(0).random` 可复现随机序列。

    Returns:
        fn() 成功时的返回值。
    """
    for n in range(max_retries + 1):          # 一共最多试 max_retries + 1 次
        try:
            return fn()                       # ★ 成功就返回，一秒都不等
        except retry_on as e:
            if n == max_retries:              # 次数用尽 → 原样抛出（别吞，上层要看真因）
                raise
            wait = base_delay * 2 ** n        # ★ 核心就这一行：1 → 2 → 4 → 8 …
            wait = wait + wait * jitter()     # ★ 加抖动：等待落在 [wait, 2*wait)
            if on_retry is not None:
                on_retry(n + 1, wait, e)      # 想记日志/发事件就挂这里
            sleep(wait)


# ============================================================
#  死循环检测：滑动窗口
# ============================================================
def _digest(tool_name: str, args: str) -> str:
    """把 (工具名, 参数) 压成一个定长指纹。

    为什么是哈希而不是直接存原文：
        - 等长、好比较，窗口里不会越存越大
        - 参数里可能有敏感内容，不留原文

    ★ 必须用 hashlib（内容哈希，跨进程稳定），**不能**用内置 hash()
      （实测：同一个字符串，两次进程结果不同）。
    ★ 用 '\\x00' 分隔，避免 "ab"+"c" 和 "a"+"bc" 拼出同一个字符串。
    """
    return hashlib.md5(f'{tool_name}\x00{args}'.encode('utf-8')).hexdigest()[:16]


class LoopDetector:
    """记住最近 window 次调用的指纹；最近 window 次全一样 → 抛 LoopDetected。

        d = LoopDetector(window=3)
        d.check('now', '{}')      # 第 1 次：放行
        d.check('now', '{}')      # 第 2 次：放行
        d.check('now', '{}')      # 第 3 次：抛 LoopDetected

    语义：**最近 window 次全都一样**才报（中间换过参数就放过 —— 说明模型在换策略）。
          这样误报少；代价是"隔一次重复一次"的那种循环检测不到（够用了）。

    ⚠️ 合法的重复也存在（反复读同一个文件、轮询等结果），所以：
        - 只对"完全相同"的调用报警
        - 抛出后主循环要【告诉模型为什么被打断】，让它换策略，而不是静默结束
    """

    def __init__(self, window: int = 3):
        """
        Args:
            window: 看最近几次。window=3 表示"连着 3 次一模一样就报"。
        """
        self.window = window
        # deque(maxlen=N)：一个自动截断的队列 —— 塞满 N 个之后，再 append 会挤掉最老的。
        # 正好就是"只记最近 N 次"，不用自己 pop(0)。
        self._recent: deque = deque(maxlen=window)

    def check(self, tool_name: str, args: str) -> None:
        """记一次调用；判定为死循环就抛 LoopDetected。

        ★ 顺序是"先记录、再判断"：所以 window=3 时，第 3 次调用就抛。
        """
        self._recent.append(_digest(tool_name, args))
        if len(self._recent) == self.window and len(set(self._recent)) == 1:
            # 最近 window 次全一样 → set 去重后只剩 1 个元素
            raise LoopDetected(tool_name, args, times=self.window)

    def reset(self) -> None:
        """清空窗口。新的一轮 run 开始时调（否则上一轮的记录会影响这一轮）。"""
        self._recent.clear()

    # 小彩蛋：给调试用，看看窗口里现在是什么
    @property
    def recent(self) -> list:
        return list(self._recent)


# ============================================================
#  自己跑一遍看效果
# ============================================================
if __name__ == '__main__':
    print('=== ① 重试：失败两次后成功（睡眠换成打印，不真等） ===')
    calls = 0

    def flaky():
        global calls
        calls += 1
        if calls < 3:
            raise TimeoutError(f'第{calls}次：网络抖了')
        return f'第{calls}次：成功！'

    def fake_sleep(s):
        print(f'      …等 {s}s')

    print('   结果:', with_retry(flaky, sleep=fake_sleep))

    print()
    print('=== ② 重试：一直失败 ===')
    try:
        with_retry(lambda: (_ for _ in ()).throw(TimeoutError('连不上')),
                   max_retries=2, sleep=fake_sleep)
    except TimeoutError as e:
        print('   最后抛出:', e)

    print()
    print('=== ③ 止损：连着 3 次同一工具同一参数 ===')
    d = LoopDetector(window=3)
    for i in range(1, 5):
        try:
            d.check('now', '{}')
            print(f'   第{i}次 now({{}}) -> 放行')
        except LoopDetected as e:
            print(f'   第{i}次 now({{}}) -> 拦住！{e}')
            break

    print()
    print('=== ④ 止损：中间换过参数，不拦 ===')
    d2 = LoopDetector(window=3)
    for i, (t, a) in enumerate([('now', '{}'), ('add', '{}'), ('now', '{}'), ('now', '{}')], 1):
        try:
            d2.check(t, a)
            print(f'   第{i}次 {t}({a}) -> 放行')
        except LoopDetected as e:
            print(f'   第{i}次 {t}({a}) -> 拦住！{e}')
