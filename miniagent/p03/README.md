# P03 —— 摘要压缩（summarize / compress）

## 目标

P02 的 `truncate` 是**直接丢掉**老消息。P03 更聪明：**把老消息压成一条摘要**，保住关键事实。

```python
# P02：直接丢
[system, user(旧), assistant(旧), user(新)]  →  [system, user(新)]          关键信息没了

# P03：先摘要
[system, user(旧), assistant(旧), user(新)]  →  [system, 摘要, user(新)]    信息保住了
```

## 你要写（`core/context.py` 里新增两个函数）

```python
def summarize(messages: list[Message], llm) -> str:
    """把一批消息交给 LLM，压成一段摘要，返回摘要字符串。"""

def compress(messages: list[Message], llm, max_tokens: int) -> list[Message]:
    """超预算时先用摘要压缩，而不是直接丢。"""
```

## 规则

**`summarize(messages, llm)`**
1. 把 `messages` 拼成一段文本，塞进一个 prompt
2. 用 `llm.chat([...])` 拿到回复
3. 返回回复的 `content`（strip 过）

> prompt 里要交代清楚：**保留关键事实（人名/数字/结论/决定），丢掉寒暄、重复。**

**`compress(messages, llm, max_tokens)`**
1. 总量 **没超** `max_tokens` → **原样返回，一次 LLM 都不调**
2. 超了 → 用 `truncate` 先算出"哪些会留下"，**被丢掉的**那部分交给 `summarize`
3. 把摘要做成**一条消息**，插在**所有 system 之后**
4. 再检查一遍：还超 → 再 `truncate` 一次兜底
5. **`summarize` 抛异常 → 直接降级成 `truncate` 的结果，绝不能崩**

## 验收

```powershell
.venv\Scripts\python.exe -m pytest miniagent/p03/test_p03.py -v
# 或
.venv\Scripts\python.exe -m miniagent.p03.test_p03
```

## 提示

- `llm` 就是个有 `chat(messages, tools=None) -> Message` 的对象（和 `LLMClient` 一样），测试里用的是假 LLM。
- 判断"哪些被丢了"：`truncate` 返回的是**同一批 Message 对象**，所以可以用 `id()` 比对。
- 别在 `compress` 里重复实现一遍裁剪逻辑 —— **复用 `truncate`**。
