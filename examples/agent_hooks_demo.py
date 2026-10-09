from build.lib.qwen_agent.llm import get_chat_model
from miniagent.core.hooks import EventBus, EventType
from enum import Enum
from typing import Callable, Dict, List
class EventType(Enum):
    RUN_START  = 'run_start'      # 整轮任务开始
    LLM_START  = 'llm_start'      # 准备问模型
    LLM_END    = 'llm_end'        # 模型回话了
    TOOL_START = 'tool_start'     # 准备调工具
    TOOL_END   = 'tool_end'       # 工具返回了
    STEP_END   = 'step_end'       # 一步结束
    RUN_END    = 'run_end'        # 整轮结束
bus = EventBus()
def llmstart():
    print("开始调用大模型了")
def toolstatr(**kwargs):
    print("开始执行工具了")
def runend(**kwargs):
    print("工具顺利结束")
bus.subscribe("llm_start",llmstart())
bus.subscribe("tool_start",toolstatr())
bus.subscribe("run_end",runend())

llm=get_chat_model={"qwen":"qwen3_max"}
from miniagent.tools import demo_tools
from miniagent.core.agent import  Agent
DF_Agent=Agent(
        llm,
        '你是一个智能助手',
        tools=['now','add'],
        max_steps=10
)
DF_Agent.run("现在几点了")
bus.emit("llm_start")
