# P02 —— 上下文截断（context.truncate）

## 目标

消息太多 / 太长时，从**最老的**开始裁，保住 `system` 和**最后一条 user**。

## 你要写（`p02/context.py`）

```python
def estimate_tokens(text: str) -> int: ...
def truncate(messages: list[Message], max_tokens: int) -> list[Message]: ...
```

## 规则（5 条）

1. **`system` 消息永不裁**
2. **最后一条 `user` 消息永不裁**
3. 其余消息**从最老开始删**（= 尽量留新的）
4. **裁完不能出现"孤立的 function 消息"**
   - 一条 `function` 消息，如果它前面那条 `assistant`（带 function_call 的）被删了，那这条 `function` 也必须删
   - 否则喂给模型时 API 会报错
5. **保底**：光 `system` + 最后一条 `user` 就超预算的话，允许超（不能裁空）

## 验收

```powershell
.venv\Scripts\python.exe -m pytest miniagent/p02/test_p02.py -v
# 或
.venv\Scripts\python.exe -m miniagent.p02.test_p02
```

全绿 = 通过。

## 提示

- 这个模块**没有参考实现**（`core/` 里没有 context.py），从零写。
- 测试把 `estimate_tokens` 换成了「1 字符 = 1 token」方便算数，所以你实现得粗糙一点也没关系，别去调 tokenizer。
- 思路建议：先算出"必须留的"占多少，再从**最新往最老**扫，能塞下就塞；最后修一遍孤立的 function。
