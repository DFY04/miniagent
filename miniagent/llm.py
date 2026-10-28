"""LLM 适配器：把 MiniAgent 的消息，翻译成「OpenAI 兼容接口」能懂的请求。

为什么不再依赖 qwen-agent：
  原来这里直接 import qwen_agent 的模型层。抽离成独立项目后，我们只需要
  「发一段对话给模型、拿回一段回答（可能带工具调用）」这一件事，用 Python
  自带的 urllib 发 HTTP 请求就能做到：少一个依赖、少一层黑盒。

支持哪些服务：
  - DashScope（通义千问）：走它的 OpenAI 兼容地址
  - DeepSeek：本身就是 OpenAI 兼容接口
  - 任何自建 OpenAI 兼容服务：用 base_url 覆盖

对外接口（Agent 只认这个）：
    client = LLMClient(build_llm_cfg(provider='deepseek'))
    msg = client.chat(messages, tools=[...])   # -> Message（可能带 function_call）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from miniagent.core.message import FunctionCall, Message

# ============================================================================
# 供应商配置：换供应商只改这张表
# ============================================================================
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "dashscope": {
        "default_model": "qwen-max",
        "model_type": None,
        "model_server": "dashscope",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
    "deepseek": {
        # 必须是 deepseek-chat：deepseek-reasoner(R1) 不支持工具调用
        "default_model": "deepseek-chat",
        "model_type": "oai",
        "model_server": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
}

DASHSCOPE_COMPATIBLE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def build_llm_cfg(
    model: Optional[str] = None,
    provider: str = "dashscope",
    *,
    base_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
    api_key: Optional[str] = None,
    require_key: bool = True,
) -> Dict[str, Any]:
    """把「哪个供应商 + 哪个模型」拼成一个配置字典。

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
        raise ValueError(f"未知的 provider：{provider!r}（支持：{sorted(PROVIDERS)}）")

    spec = PROVIDERS[provider]
    model = model or spec["default_model"]
    env_name = api_key_env or spec["api_key_env"]
    key = api_key if api_key is not None else os.getenv(env_name)

    if require_key and not key:
        raise ValueError(
            f"缺少 API key：请在项目根 .env 里设置 {env_name}"
            f"（或用 --api-key-env 指定别的变量名）"
        )

    cfg: Dict[str, Any] = {
        "model": model,
        "model_server": base_url or spec["model_server"],
    }
    if spec["model_type"]:
        cfg["model_type"] = spec["model_type"]
    if key:
        cfg["api_key"] = key
    return cfg


# ============================================================================
# 消息格式转换：MiniAgent Message <-> OpenAI 的 messages
# ============================================================================
def _to_openai_messages(messages: List[Message], call_ids: List[str]) -> List[dict]:
    """把 MiniAgent 的消息列表转成 OpenAI 兼容的 messages。

    call_ids 是「已经发出去的工具调用 id」队列：OpenAI 的 tool 消息要带
    tool_call_id 指回是哪次调用，而 MiniAgent 的 FunctionCall 里没有 id，
    所以我们自己生成、按顺序配对。
    """
    out: List[dict] = []
    next_id = len(call_ids)
    for m in messages:
        if m.role == "function":
            call_id = call_ids[-1] if call_ids else f"call_{next_id}"
            out.append({"role": "tool", "tool_call_id": call_id, "content": m.content or ""})
        elif m.role == "assistant" and m.function_call:
            call_id = f"call_{next_id}"
            next_id += 1
            call_ids.append(call_id)
            out.append({
                "role": "assistant",
                "content": m.content or "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": m.function_call.name, "arguments": m.function_call.arguments},
                }],
            })
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


def _from_openai_message(data: dict) -> Message:
    """把 OpenAI 返回的 message 转成 MiniAgent 的 Message。"""
    content = data.get("content") or ""
    calls = data.get("tool_calls") or []
    fc = None
    if calls:
        fn = calls[0].get("function") or {}
        if fn.get("name"):
            fc = FunctionCall(name=fn["name"], arguments=fn.get("arguments") or "{}")
    return Message.assistant(content, function_call=fc)


# ============================================================================
# 客户端
# ============================================================================
class LLMClient:
    """对 Agent 暴露统一的 `chat(messages, tools) -> Message` 接口。"""

    def __init__(self, cfg: dict):
        self.model: str = cfg["model"]
        self.api_key: Optional[str] = cfg.get("api_key")
        self._server: str = cfg.get("model_server") or ""
        self.temperature: float = float(cfg.get("temperature", 0.0))
        self.timeout: float = float(cfg.get("timeout", 120.0))
        self.retries: int = int(cfg.get("retries", 2))
        self._call_ids: List[str] = []

    def _base_url(self) -> str:
        if self._server in ("", "dashscope"):
            return DASHSCOPE_COMPATIBLE_URL
        return self._server.rstrip("/")

    def _post(self, body: dict) -> dict:
        url = self._base_url().rstrip("/") + "/chat/completions"
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        最后错误: Exception | None = None
        for 尝试 in range(self.retries + 1):
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                # 4xx 是我们自己的问题（key/参数），重试没用，直接报错
                if 400 <= exc.code < 500:
                    raise RuntimeError(f"模型接口返回 {exc.code}：{detail}") from exc
                最后错误 = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                最后错误 = exc
            if 尝试 < self.retries:
                time.sleep(2 ** 尝试)
        raise RuntimeError(f"调用模型失败（已重试 {self.retries} 次）：{最后错误}")

    def chat(self, messages: List[Message], tools: Optional[List[dict]] = None) -> Message:
        """发一轮对话，返回模型回复（可能是自然语言，也可能是工具调用请求）。"""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": _to_openai_messages(messages, self._call_ids),
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                }}
                for t in tools
            ]

        data = self._post(body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"模型返回格式看不懂：{str(data)[:300]}") from exc
        return _from_openai_message(message)
