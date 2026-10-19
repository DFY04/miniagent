"""LLM 适配器：复用 qwen-agent 的模型封装，只做「消息格式转换」。

设计原则：不重造 LLM HTTP 层。qwen-agent 已经处理好 DashScope / OpenAI 兼容
接口、流式、function calling 的方言差异，我们把它包一层即可。

转换方向：
    miniagent Message  <--(适配器)-->  qwen_agent Message
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from qwen_agent.llm import get_chat_model
from qwen_agent.llm.schema import FunctionCall as QFunctionCall
from qwen_agent.llm.schema import Message as QMessage

from miniagent.core.message import FunctionCall, Message


# ---------------- 格式转换 ----------------
def _to_qwen(m: Message):
    fc = None
    if m.function_call:
        fc = QFunctionCall(name=m.function_call.name, arguments=m.function_call.arguments)
    return QMessage(role=m.role, content=m.content or '', name=m.name, function_call=fc)


def _from_qwen(qm) -> Message:
    content = qm.content
    if isinstance(content, list):  # 多模态 content，M1 只取文本
        content = '\n'.join(getattr(x, 'text', None) or '' for x in content)

    fc = None
    if getattr(qm, 'function_call', None):
        fc = FunctionCall(name=qm.function_call.name, arguments=qm.function_call.arguments)

    return Message(role=qm.role, content=content or '', name=getattr(qm, 'name', None), function_call=fc)


class LLMClient:
    """对 Agent 暴露统一的 `chat(messages, tools) -> Message` 接口。"""

    def __init__(self, cfg: dict):
        self.model = get_chat_model(cfg)

    def chat(self, messages: List[Message], tools: Optional[List[dict]] = None) -> Message:
        qwen_messages = [_to_qwen(m) for m in messages]
        # qwen-agent 里全用真值判断（`if functions:`），所以空列表 [] 和 None 等价；
        # 这里显式转 None，只是让「没有工具」的意图更清楚。
        functions = tools or None

        last = None
        for chunk in self.model.chat(messages=qwen_messages, functions=functions, stream=True):
            last = chunk
        if not last:
            raise RuntimeError('LLM 没有返回任何内容')
        return _from_qwen(last[-1])


# ============================================================================
#  供应商配置
# ============================================================================
#
#  ★ 换供应商只需要换一个 dict —— 这就是"不重造 LLM 层"的回报：
#     qwen-agent 已经处理好了 DashScope / OpenAI 兼容接口的方言差异。
#
#  ★ 加新供应商只改这张表，cli.py 不用动。

PROVIDERS: Dict[str, Dict[str, Any]] = {
    'dashscope': {
        'default_model': 'qwen-max',
        # dashscope 是 qwen-agent 的默认路径，不需要 model_type
        'model_type': None,
        'model_server': 'dashscope',
        'api_key_env': 'DASHSCOPE_API_KEY',
    },
    'deepseek': {
        # ★ 必须是 deepseek-chat：deepseek-reasoner(R1) 不支持 function calling，
        #   而本项目的 Agent 核心就是靠 function_call 驱动工具，用 R1 工具全废。
        'default_model': 'deepseek-chat',
        'model_type': 'oai',          # 复用 qwen-agent 的 OpenAI 兼容实现
        'model_server': 'https://api.deepseek.com/v1',
        'api_key_env': 'DEEPSEEK_API_KEY',
    },
}


def build_llm_cfg(
    model: Optional[str] = None,
    provider: str = 'dashscope',
    *,
    base_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
    api_key: Optional[str] = None,
    require_key: bool = True,
) -> Dict[str, Any]:
    """把 "哪个供应商 + 哪个模型" 拼成 get_chat_model 认的 cfg。

    Args:
        model:       模型名；不传就用该 provider 的默认模型
        provider:    PROVIDERS 里的键
        base_url:    覆盖服务地址（自建 OpenAI 兼容服务时用）
        api_key_env: 从哪个环境变量读 key（默认按 provider 推）
        api_key:     直接给 key（优先于环境变量）
        require_key: False 则允许没有 key（单元测试用）

    Raises:
        ValueError: provider 不存在，或 require_key=True 但找不到 key
    """
    if provider not in PROVIDERS:
        raise ValueError(
            f'未知的 provider：{provider!r}（支持：{sorted(PROVIDERS)}）')

    spec = PROVIDERS[provider]
    model = model or spec['default_model']
    env_name = api_key_env or spec['api_key_env']
    key = api_key if api_key is not None else os.getenv(env_name)

    if require_key and not key:
        raise ValueError(
            f'缺少 API key：请在项目根 .env 里设置 {env_name}'
            f'（或用 --api-key-env 指定别的变量名）')

    cfg: Dict[str, Any] = {
        'model': model,
        'model_server': base_url or spec['model_server'],
    }
    if spec['model_type']:
        cfg['model_type'] = spec['model_type']
    if key:
        cfg['api_key'] = key
    return cfg
