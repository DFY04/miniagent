"""Agent 主循环（带事件埋点版）。

和 core/agent.py 是同一套 think → act → observe 循环，区别只在于这里会在关键节点
emit 事件：tracer / budget / retry / approval / rollback 这些横切能力都以订阅者
的形式挂到 EventBus 上，主循环本身不写死它们。

埋点位置（7 处）：
    RUN_START   → run() 开头
    LLM_START   → 每次问模型之前
    LLM_END     → 模型回话之后
    TOOL_START  → 每次执行工具之前
    TOOL_END    → 工具返回之后
    STEP_END    → 一步结束
    RUN_END     → 任务结束（done / max_steps）
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Union

import time
from miniagent.memory.vector_store import DashScopeChromaRAG
from miniagent.core.hooks import ApprovalRejected, EventType
from miniagent.core.message import FunctionCall, Message
from miniagent.core.tool import TOOL_REGISTRY, BaseTool

from miniagent.memory.short_term import ShortTermMemory
from miniagent.runtime.budget import BudgetExceeded, Budget  # ★ P06：预算超限的异常（ControlFlowSignal）
from miniagent.runtime.retry import LoopDetected, with_retry  # ★ P07：重试 + 死循环检测
class Agent:
    """一个最小的工具调用 Agent（带 hooks）。

    llm 需要实现 `chat(messages, tools=None) -> Message`。
    bus 不传也能用 —— 那样就一个事件都不发。
    """

    def __init__(
        self,
        llm,
        system_message: str = '',
        tools: Optional[List[Union[str, dict, BaseTool]]] = None,
        max_steps: int = 10,
        mem=None,
        approve=None,
        bus=None,
        rag=None,
        budget=None,                             # ★ P06：预算（不传 = 不限制）
        loop_detector=None,                      # ★ P07：死循环检测器（不传 = 不检测）
        max_retries: int = 3,                    # ★ P07：LLM 调用失败最多重试几次（0 = 一次都不重试）
        base_delay: float = 1.0,                 # ★ P07：退避基数（1 → 2 → 4 秒）
        sleep=time.sleep,                        # ★ P07：可注入的睡眠函数（测试传假的，免得真等）
        ckpt=None,                               # ★ P10：检查点（不传 = 不存档、不回滚）
        rollback_gate=None,                      # ★ P10：回滚决策来源（不传 = 不问、不回滚）
        max_rollbacks: int = 3,                  # ★ P10：最多回滚几次（防"滚回开头又跑一遍"）

    ):
        self.approve=approve
        self.mem=mem
        self.llm = llm
        self.system_message = system_message
        self.max_steps = max_steps
        self.bus = bus
        self.budget=budget
        self.rag=rag# ★ 新增
        self.loop_detector = loop_detector       # ★ P07：只要有 .check(tool_name, args) 就行（鸭子类型）
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        # ★ P10：两个都默认 None —— 不传 = 整套回滚能力彻底不参与，老行为一字不变
        self.ckpt = ckpt
        self.rollback_gate = rollback_gate
        self.max_rollbacks = max_rollbacks
        self.tool_map: Dict[str, BaseTool] = {}
        for t in (tools or []):
            self.register_tool(t)
        if self.mem is not None and self.system_message:
            self.mem.add(Message.system(self.system_message))
        if self.rag is not None:                 # ★ 判空：不传 rag 就不建库
            self.rag.add_documents()
        if self.budget is not None and self.bus is not None:
            self.budget.attach(self.bus)
    # ---------------- 工具挂载（第二级注册表） ----------------
    def register_tool(self, t: Union[str, dict, BaseTool]) -> None:
        if isinstance(t, BaseTool):
            self.tool_map[t.name] = t
        elif isinstance(t, str):
            self.tool_map[t] = self._instantiate(t, cfg=None)
        elif isinstance(t, dict):
            name = t.get('name')
            if not name:
                raise ValueError(f'工具配置缺少 name 字段: {t}')
            self.tool_map[name] = self._instantiate(name, cfg=t)
        else:
            raise TypeError(f'不支持的工具类型: {type(t)}')

    @staticmethod
    def _instantiate(name: str, cfg: Optional[dict]) -> BaseTool:
        if name not in TOOL_REGISTRY:
            raise ValueError(f'工具 `{name}` 未注册。请确认已 import 对应模块。')
        return TOOL_REGISTRY[name](cfg)

    @property
    def tool_schemas(self) -> List[dict]:
        return [t.schema for t in self.tool_map.values()]

    # ---------------- 发事件的小助手 ----------------
    def _emit(self, event_type: EventType, **data) -> None:
        """★ 没传 bus 就什么都不做。
        有了它，run() 里就不用到处写 `if self.bus:`，可读性高很多。"""
        if self.bus:
            self.bus.emit(event_type, **data)

    # ---------------- 主循环 ----------------
    def run(self, messages: List[Message]) -> Iterator[Message]:
        """跑一次任务。逐个 yield 本轮新产生的消息。"""
        self._emit(EventType.RUN_START)                                    # ★ 1

        # ★ P10：先把“谁是被回滚的那份上下文”绑好。
        #   有 mem → 上下文就是 mem 内部那个 list（模型看的就是它）；
        #   没 mem → 上下文就是 run() 传进来的那个 list。
        #   Checkpoint 回滚时是【原地】截断这个 list（messages[:] = kept），
        #   所以只要两边指向同一个对象，回滚就是“真删了”。
        if self.ckpt is not None:
            self.ckpt.bind(self.mem.message if self.mem is not None else messages)

        # ★ P07：新的一轮任务 → 把“重复调用”的窗口清空（不复位的话，上一轮的记录会算到这一轮）。
        #   注意 LoopDetector 【不需要 attach(bus)】—— 它是由下面主循环直接调用的，
        #   不像 Budget 那样靠订阅事件来记账。所以这里直接 reset 就行。
        if self.loop_detector is not None:
            self.loop_detector.reset()

        # ---- 长期记忆：一轮只检索一次（留在循环外，省 API）----
        # ★ 埋点必须贴着检索走：检索挪到哪儿，RAG_START / RAG_END 就跟着挪到哪儿。
        #   留在循环里会变成"假情报"：① max_steps=0 时检索发生了却一个事件都不发；
        #   ② 就算发出来，也是在检索已经结束之后才补发，时序是错的。
        query = messages[-1].content if messages else ''
        self._emit(EventType.RAG_START, query=query)
        docs: List[str] = []
        if self.rag is not None and query:
            try:
                rag_reply = self.rag.search(query) if (self.rag and query) else {}
            except Exception as e:
                self._emit(EventType.RAG_END, query=query, hits=0, error=str(e))
                rag_reply = {}
            docs = (rag_reply.get('documents') or [[]])[0]

        self._emit(EventType.RAG_END, query=query, hits=len(docs))

        for m in messages:
            if self.mem is not None:             # ★ 判空
                self.mem.add(m)

        # ★ P10：循环从 for 换成 while —— 因为回滚要求 step 【能被改小】。
        #   for step in range(n)：step 的所有权在 range，回滚后下一轮它又被赋回递增的值，
        #                        等于白回滚。
        #   while：step 的所有权在我们手里，回滚后 step = ckpt.step 就真的拨回去了。
        #   （ckpt 不传时 start=0，行为和原来的 range(max_steps) 完全一致。）
        step = self.ckpt.step if self.ckpt is not None else 0
        # ★ 上限写成 start + max_steps（而不是裸的 max_steps）：
        #   这样“重启继续跑”也能再跑满 max_steps 步，
        #   和 checkpoint.py 文档里的 range(start, start + max_steps) 对齐。
        #   另：end 在循环前定死，所以“回滚重跑”也是要花预算的 —— 不会因为回滚而白送步数。
        end = step + self.max_steps
        rollbacks = 0
        while step < end:
            # ★ P10：唯一时钟 —— 把循环变量照进 Checkpoint。
            #   必须在【这一轮任何 snapshot_file 之前】调：snapshot 用 ckpt.step 决定存到
            #   哪个 step-XXXX 目录，照晚了这一轮的文件会存进上一轮的目录。
            if self.ckpt is not None:
                self.ckpt.begin_step(step)

            # ---- ★ P06 预算闸门：每一步【动手之前】先查账 ----
            # 为什么放"事前"而不是"事后"：事后才发现超限，这一步的钱已经花掉了。
            # 为什么 try 在循环里（而不是包住整个 for）：
            #   ① 现在 Budget 只在 check() 里抛，循环里这一处就够；
            #   ② 不用把整个循环体再缩进一层，diff 最小。
            #   以后若改成"在事件回调里直接抛"，就得把 try 提到 for 外面 ——
            #   BudgetExceeded 继承了 ControlFlowSignal，能穿过事件总线的异常隔离。

            if self.budget is not None:
                try:
                    self.budget.check()
                except BudgetExceeded as e:
                    self._emit(EventType.RUN_END, reason='budget')     # ★ 优雅中止，带上原因
                    yield Message.assistant(f'[已超出预算，任务中止] {e}')
                    return

            # ★ 修正：DashScope 只允许【一条】system，且必须在最前面。
            #   实测报错：must contain no more than one system message
            #   所以不能 self.mem.add(Message.system(...)) 再插一条，
            #   而是把资料【并进原来那条 system】（新建 Message，不改原对象！）
            history = self.mem.build() if self.mem is not None else list(messages)
            if docs:
                text = '\n'.join(f'[{i}] {d}' for i, d in enumerate(docs, 1))
                block = f'参考资料：\n{text}'
                if history and history[0].role == 'system':
                    history = [Message.system(f'{history[0].content}\n\n{block}'), *history[1:]]
                else:
                    history = [Message.system(block), *history]
            # ★ 带上 messages=history：这是本轮真正发给模型的输入 prompt，
            #   Budget 订阅 LLM_START 靠它记【输入 token】（LLM_END 只带了输出）。
            self._emit(EventType.LLM_START, step=step, messages=history)
            # ★ 2
            # ★ P07 重试：LLM 调用是“偶发失败”概率最高的地方（限流/超时/5xx）。
            #   fn 写成【零参数的 lambda】——这样 with_retry 不用管被调用者有多少种参数形状。
            #   ⚠️ mem.add(reply) 必须在【重试成功之后】才执行（就是下面那行）：
            #      否则失败的那几次也会被塞进对话历史。
            reply = with_retry(
                lambda: self.llm.chat(history, tools=self.tool_schemas),
                max_retries=self.max_retries,
                base_delay=self.base_delay,
                sleep=self.sleep,
                on_retry=self._on_llm_retry,
            )
            reply.step = step            # ★ 盖上步号（在 mem.add / yield / LLM_END 之前）
            if self.mem is not None:                 # ★ 判空
                self.mem.add(reply)
            yield reply
            self._emit(EventType.LLM_END, reply=reply)                     # ★ 3

            if not reply.function_call:
                self._emit(EventType.RUN_END, reason='done')               # ★ 4
                return  # 没有工具调用 -> 任务结束

            tool_name = reply.function_call.name
            try:
                # ★ P09：TOOL_START 必须带上 args —— 审批闸门要看到“要执行什么”
                #   才能判危险词（DROP/rm…）。只带 name 的话审批永远看不到参数。
                self._emit(EventType.TOOL_START, name=tool_name,
                           args=reply.function_call.arguments)              # ★ 5
            except ApprovalRejected as e:                                   # 审批订阅者拦下来了
                self._emit(EventType.RUN_END, reason='rejected')
                yield Message.assistant(f'[操作被拒绝] {e}')
                return
            # ★ P07 止损：_execute 里会先问一句“这个工具 + 这组参数是不是刚来过”，
            #   连着来太多次就抛 LoopDetected —— 在这里接住，优雅收尾。
            try:
                observation = self._execute(reply.function_call)
            except LoopDetected as e:
                self._emit(EventType.RUN_END, reason='loop')
                yield Message.assistant(
                    f'[检测到重复调用，已中止] {e}。\n'
                    f'同一个工具用相同参数重复调用不会得到新结果，'
                    f'请换参数、换工具，或直接回答用户。'
                )
                return
            self._emit(EventType.TOOL_END, name=tool_name, result=observation)   # ★ 6

            fn_msg = Message.function(name=tool_name, content=observation, step=step)
            if self.mem is not None:                 # ★ 判空
                self.mem.add(fn_msg)
            yield fn_msg
            self._emit(EventType.STEP_END, step=step)                      # ★ 7

            # ★ P10：回滚闸门 —— 每步结束后问一句“要不要回去”。
            #   为什么放在最后而不是开头：这一步已经完整做完了（想→做→观察），
            #   现在再问“这一步要吗”，用户的判断才有完整信息。
            if self.ckpt is not None and self.rollback_gate is not None:
                target = self.rollback_gate.decide(step)   # None = 不回滚；
                if target is not None:                     # ★ 必须 is None，0 是合法目标
                    rollbacks += 1
                    if rollbacks > self.max_rollbacks:
                        # ★ 回滚预算：回滚把 step 拨小，理论上可以“滚回开头 → 又跑一遍”
                        #   永远不结束。死循环检测（P07）拦的是“工具重复调用”，
                        #   拦不住这个。所以单独给一条上限。
                        self._emit(EventType.RUN_END, reason='rollback_budget')
                        yield Message.assistant(
                            f'[回滚次数已达上限 {self.max_rollbacks}，任务中止]'
                        )
                        return
                    self._emit(EventType.ROLLBACK, step=step, target=target)
                    self.ckpt.rollback(target)             # 两个世界一起回去
                    step = self.ckpt.step                  # ★ 时钟拨回，下一轮从这里重跑
                    continue                               # ★ 不走 step += 1：否则就白回滚了

            step += 1                                      # 正常推进（while 的步长归我们管）

        # 步数用尽仍未结束
        self._emit(EventType.RUN_END, reason='max_steps')
        yield Message.assistant(f'[已达到最大步数 {self.max_steps}，任务中止]')

    # ---------------- 重试的埋点 ----------------
    def _on_llm_retry(self, attempt: int, wait: float, err: BaseException) -> None:
        """with_retry 每次准备重试时都会调这里（相当于“退回重试”的喉兵）。"""
        self._emit(EventType.LLM_RETRY, attempt=attempt, wait=wait, error=str(err))

    # ---------------- ★ P10：工具调用的两个接线点 ----------------
    def _snapshot_writes(self, tool, args) -> None:
        """动手【之前】让 Checkpoint 把会被改的文件存一份。

        存哪些？听工具自己的声明（BaseTool.declare_writes）。
        Agent 看不懂参数，也没有必要看懂 —— 它只负责在正确的时机调一下。
        """
        if self.ckpt is None:
            return
        try:
            paths = tool.declare_writes(args)     # 只算路径，不写文件
        except Exception as e:                    # noqa: BLE001
            # ★ 存档失败不能把主循环打断 —— 少存一次档，代价只是“这一步回不去”，
            #   而抛出去就是整个任务崩掉（反正回滚本来也只是“降低代价”的手段）。
            #   ★ 这里发的是专门的 CKPT_ERROR，【不能】借 TOOL_START：
            #     审批闸门（P09）订阅的就是 TOOL_START，借它发会把审批触发两次。
            self._emit(EventType.CKPT_ERROR, name=tool.name,
                       args=args, error=f'{type(e).__name__}: {e}',
                       where='declare_writes')
            return
        for p in paths or []:
            try:
                self.ckpt.snapshot_file(p)
            except Exception as e:                # noqa: BLE001
                self._emit(EventType.CKPT_ERROR, name=tool.name, path=str(p),
                           error=f'{type(e).__name__}: {e}', where='snapshot_file')

    def _log_tool_call(self, name: str, args, observation: str) -> None:
        """动手【之后】记一笔账：这一步调了什么、得到什么。

        只记账，不推进时钟（ckpt.step 由 begin_step 统一给）。
        失败也要记 —— “工具报了错”正是最可能需要回滚的场景。
        """
        if self.ckpt is None:
            return
        try:
            self.ckpt.log_tool_call(name, args, observation)
        except Exception as e:                    # noqa: BLE001
            self._emit(EventType.CKPT_ERROR, name=name,
                       error=f'{type(e).__name__}: {e}', where='log_tool_call')

    def _execute(self, fc: FunctionCall) -> str:
        """执行一次工具调用，任何异常都转成字符串 observation（不中断 run）。"""
        # ★ P07 死循环闸门：必须放在【try 外面】！
        #   放进下面那个 try 就会被 `except Exception` 吞掉 → 止损静默失效
        #   （LoopDetected 是 ControlFlowSignal，而它就是 Exception 的子类）。
        if self.loop_detector is not None:
            self.loop_detector.check(fc.name, fc.arguments)

        if fc.name not in self.tool_map:
            return f'错误：没有名为 `{fc.name}` 的工具。可用工具：{list(self.tool_map)}'
        tool = self.tool_map[fc.name]
        self._snapshot_writes(tool, fc.arguments)     # ★ P10：动手【之前】存档
        try:
            observation = str(tool.call(fc.arguments))
        except Exception as e:  # noqa: BLE001
            # 顺手给模型一句“别再原样重试”的提示。
            # 这是防死循环的另一半：LoopDetector 负责【打断】，这句话负责【引导换策略】。
            observation = (f'工具 `{fc.name}` 执行失败：{type(e).__name__}: {e}'
                           f'（用同样的参数再试一次通常没用，请换参数或换工具）')

        # ★ P10：动手【之后】记账。写在 try 外面而不是里面：成功、失败都要记；
        #   而且换成 `return ...` 的话，账就记不上了。
        self._log_tool_call(fc.name, fc.arguments, observation)
        return observation
