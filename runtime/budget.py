from miniagent.core.context import estimate_tokens
from miniagent.core.hooks import ControlFlowSignal, EventType


class BudgetExceeded(ControlFlowSignal): ...


class Budget:
    def __init__(self, max_steps=None, max_tokens=None, max_cost_usd=None, price_per_1k=0.0014):
        self.max_steps, self.max_tokens, self.max_cost_usd = max_steps, max_tokens, max_cost_usd
        self.price_per_1k = price_per_1k
        self.used_steps = 0
        self.used_tokens = 0
        self.used_cost = 0.0
        self._attach_buses = []

    def attach(self, bus) -> None:
        if bus in self._attach_buses:  # ② 查本子：挂过就撤
            return
        self._attach_buses.append(bus)
        # ★ 三个都要：subscribe(事件类型, 处理函数)；只有通配订阅才收 event
        bus.subscribe(EventType.RUN_START, self.on_run_start)  # 新 run → 清零
        bus.subscribe(EventType.LLM_END, self.on_llm_end)  # 记账：token
        bus.subscribe(EventType.STEP_END, self.on_step_end)  # 记账：步数

    def on_run_start(self, **kw) -> None:  # ★ 只有 **kw
        self.used_steps = 0
        self.used_tokens = 0
        self.used_cost = 0.0

    def on_llm_end(self, **kw) -> None:  # ★ 只有 **kw
        reply = kw.get('reply')
        text = getattr(reply, 'content', '') or ''
        # ★ 修：先算【本次增量】，再用增量算钱。
        #   原来写的是 self.used_tokens（累计值）—— 等于把前面的量反复算进去，
        #   成本会呈平方级偏高（调用 3 次偏 2 倍，100 次偏约 50 倍）。
        n = estimate_tokens(text)                 # 本次输出 token（估算值，未含输入）
        self.used_tokens += n
        self.used_cost += n / 1000 * self.price_per_1k

    def on_step_end(self, **kw) -> None:  # ★ 只有 **kw
        self.used_steps += 1

    def check(self) -> None:
        if self.max_tokens is not None and self.used_tokens > self.max_tokens:
            raise BudgetExceeded(f'token 超限: {self.used_tokens} > {self.max_tokens}')
        if self.max_steps is not None and self.used_steps >= self.max_steps:
            raise BudgetExceeded(f'步数超限: {self.used_steps} >= {self.max_steps}')
        if self.max_cost_usd is not None and self.used_cost > self.max_cost_usd:
            raise BudgetExceeded(f'成本超限: {self.used_cost:.4f} > {self.max_cost_usd}')