# mypy 详解 —— 在运行之前就发现类型错误

> 配套：`miniagent/mypy.ini`、`miniagent/core/protocols.py`
> 本文所有例子都在这个项目里实测过。

---

## 0. 一句话

**mypy 是 Python 的静态类型检查器：不运行代码，光读代码就告诉你"这里的类型不对"。**

```python
def summarize(messages, llm):
    reply = llm.chat(Message.user(prompt))     # ← mypy: 这里传错了，应该是 list[Message]
```

**不用跑，不用等测试挂，写的时候就标出来。**

---

## 1. 为什么需要它

Python 是动态类型语言，**类型错误只在运行时才暴露**：

```python
def f(x: int) -> int:
    return x + 1

f('abc')      # 定义时不报错，调用时也不报错
              # 只有真的执行到 'abc' + 1 才炸：
              # TypeError: can only concatenate str (not "int") to str
```

**更糟的是**：运行时错误还可能被 `try/except` 吞掉，变成"看起来没事"。

你在 P03 就踩过：

```
5 passed:
  ✅ falls_back_when_llm_raises      ← 走降级（正常）
  ✅ keeps_last_user_message         ← 其实也走了降级！summarize 抛 TypeError 被吞了
  ✅ respects_budget                 ← 同上
```

`summarize` 里 `llm.chat(Message.user(prompt))` 少了一对方括号 → 抛 `TypeError` → 被降级逻辑吃掉 → **测试"通过"了，但摘要那条路根本没跑**。

**如果当时有 mypy，这行根本写不出来。**

---

## 2. 静态检查 vs 运行时检查

| | 谁做 | 什么时候 | 能查什么 |
|---|---|---|---|
| Python 自己 | 字节码编译器 | 加载 `.py` 时 | **只查语法** |
| Python 自己 | 解释器 | 运行时 | 语法 + 类型（但已经晚了） |
| **mypy** | 独立工具 | **根本不用运行** | **类型** |

所以：

```python
for i in range(len(must))       # ❌ 少了冒号 → Python 立刻报（语法）
llm.chat(Message.user(prompt))  # ❌ Python 不报！跑到才炸（类型）
```

**这就是为什么前者当场红，后者要等到运行时。**

---

## 3. 安装与运行

### 装到项目里

```powershell
uv pip install --python E:/agent-study/qwen-agent/.venv/Scripts/python.exe mypy
```

### 跑

```powershell
cd E:\agent-study\qwen-agent
.venv\Scripts\python.exe -m mypy --config-file miniagent/mypy.ini miniagent/core/*.py
```

### 输出长这样

```
miniagent/core/context.py:76:20: error: Argument 1 to "chat" of "ChatModel" has
incompatible type "Message"; expected "list[Message]"  [arg-type]
        reply=llm.chat(Message.user(prompt))
                       ^~~~~~~~~~~~~~~~~~~~
Found 1 error in 1 file (checked 8 source files)
```

格式：`文件:行:列: error: 说明 [错误码]` + **把出错那行代码和位置也标出来**。

---

## 4. 注解是给谁看的？

**注解不是给 Python 看的。**

```python
def f(x: int) -> int:
    return x + 1

f('abc')      # 跑得毫无怨言（直到 x + 1 那一步）
```

- Python 运行时**不检查**注解，只把它们存进 `__annotations__`
- 注解的真正读者：**mypy / PyCharm / 你的同事 / 未来的你**
- 所以注解的价值 = **能被工具读，从而在运行前抓错**

> `from __future__ import annotations` 会让注解变成字符串（延迟求值），mypy 照样能读。

---

## 5. 常用类型速查

```python
from typing import Optional, Union, Callable, Any, Literal, Protocol

# 基础
x: int = 1
name: str = 'a'
ok: bool = True

# 容器（Python 3.9+ 可以直接用小写）
names: list[str] = ['a', 'b']
scores: dict[str, int] = {'a': 1}
pair: tuple[str, int] = ('a', 1)

# 可选（可能是 None）★ 最常用
def f(x: Optional[int] = None):      # 等价于 int | None
    ...

# 联合类型
def g(x: int | str):                 # 要么 int 要么 str
    ...

# 可调用（函数当参数）
def apply(fn: Callable[[int], str], x: int) -> str:
    return fn(x)

# 字面量（只允许这几个具体值）
mode: Literal['auto', 'manual'] = 'auto'

# Any：放弃检查（mypy 遇到 Any 就摆烂）
def h(x: Any): ...                   # ← 这一片全是盲区，少用

# 类型别名
MessageList = list['Message']
```

**`Any` 是最大的坑**：一个参数没注解，就等于 `Any`，mypy 对它**什么都不查**。

```python
def summarize(messages: list[Message], llm) -> str:              # ❌ llm = Any，放弃检查
def summarize(messages: list[Message], llm: ChatModel) -> str:   # ✅ 会检查
```

**实测证据**（同一个错误的函数，一个有注解一个没有）：

```
① 没注解：mypy 对 llm.chat(...) 传错类型【一字不说】
② 有注解：mypy 当场揪出来
   error: Argument 1 to "chat" of "ChatModel" has incompatible type
          "Message"; expected "list[Message]"  [arg-type]
```

---

## 6. Protocol —— 鸭子类型（重点）

**问题**：`llm` 可能是 `LLMClient`，也可能是测试里的 `FakeLLM`，怎么写注解？

**答案**：不写具体类，写"**长什么样**"。

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ChatModel(Protocol):
    def chat(self, messages: list[Message],
             tools: Optional[list[dict]] = None) -> Message: ...
```

**规则：只要有这个方法（参数和返回值对得上），就算符合 `ChatModel`。**

- ✅ `LLMClient` 符合（它真有这个方法）
- ✅ 测试里的 `FakeLLM` 符合
- ✅ 你不用让它们继承任何东西

> 这就是"**鸭子类型**"：看起来像鸭子、叫起来像鸭子，那就是鸭子。
> Java 要显式 `implements`，Python 的 Protocol 不用。

`runtime_checkable` 的额外好处：能用 `isinstance(obj, ChatModel)`（只检查方法存在，不检查签名）。

**Protocol 在哪定义**：`miniagent/core/protocols.py`

**为什么这个特别适合 agent 项目**：LLM、工具、订阅者、检索器……全是"只要长得像就行"的东西，用 Protocol 描述契约最自然，而且**测试可以塞假实现**。

---

## 7. 配置：`miniagent/mypy.ini`

```ini
[mypy]
python_version = 3.12

check_untyped_defs = True       # 连"没写注解的函数体"也检查
no_implicit_optional = True     # 不允许 None 隐式当 Optional
show_error_codes = True         # 报错带错误码（方便上网搜）
show_column_numbers = True
pretty = True
warn_unused_ignores = True      # 写了 # type: ignore 但没用上 → 提醒你删
warn_redundant_casts = True

ignore_missing_imports = True   # 第三方库没类型存根，别报它
follow_imports = silent         # 跟进依赖但不报它们的错

# 我们自己的代码要查
[mypy-miniagent.*]
ignore_errors = False

# examples / tests / 练习目录里会故意写错，别太严
[mypy-miniagent.examples.*]
ignore_errors = True
```

**关键两条**：

| 配置 | 作用 |
|------|------|
| `ignore_missing_imports` | 不因为 `sympy`/`dotenv` 没有类型存根就报错 |
| `follow_imports = silent` | 分析依赖，但**不报依赖内部的错** ← 这条过滤掉了 qwen_agent 的一大堆噪音 |

**效果对比**：

```
不加配置：  30+ 行错误（全是 qwen_agent 自己的）+ 你的 1 个
加了配置：  miniagent/core/context.py:76 error: ...[arg-type]
            Found 1 error in 1 file   ← 干净
```

---

## 8. 常见错误码速查

| 错误码 | 意思 | 典型场景 |
|--------|------|---------|
| `[arg-type]` | 传参类型不对 | `chat(Message)` vs `chat(list[Message])` ← 你的 bug |
| `[return-value]` | 返回值类型不对 | 签名 `-> str`，return 了 `Message` |
| `[assignment]` | 赋值类型不对 | `x: int = 'abc'` |
| `[attr-defined]` | 对象没这个属性 | `Message.user(...)` 但没定义 `user` |
| `[union-attr]` | `Optional` 没判空就取属性 | `x: Optional[C]`，直接 `x.attr` |
| `[call-arg]` | 参数个数/名字不对 | 少传一个必填参数 |
| `[no-untyped-def]` | 函数没写注解（`--strict` 才报） | `def f(x): ...` |
| `[import-not-found]` | 找不到模块 | 拼错模块名 |

**`Optional` 那个是新手最容易踩的**：

```python
def f(x: Optional[Message]) -> str:
    return x.content          # ❌ Item "None" of "Optional[Message]" has no attribute "content"

    if x is None:             # ✅ 先判空
        return ''
    return x.content
```

---

## 9. 渐进式采用（重要）

**别一上来就 `--strict`** —— 几千个错误会让人放弃。

三个阶段：

```
阶段 1：只检查"有注解的部分"          ← 你的现状
阶段 2：加 check_untyped_defs = True   ← 也开始查没注解的函数体
阶段 3：--strict                       ← 全部严格
```

而且可以**按目录放开**：

```ini
[mypy-miniagent.core.*]
ignore_errors = False        # 核心代码严查

[mypy-miniagent.examples.*]
ignore_errors = True         # 示例代码放水
```

---

## 10. 在 PyCharm 里配成"边写边报"

**① 装插件**

```
Settings → Plugins → Marketplace → 搜 "mypy" → Install → Restart
```

**② 指向 mypy 和配置**

```
Settings → Tools → mypy
  mypy executable : E:\...\.venv\Scripts\mypy.exe
  config file     : E:\...\miniagent\mypy.ini
```

**③ 内置检查也调严**

```
Settings → Editor → Inspections → 搜 "Type"
  → Python → "Type checker"          Severity: Error
  → Python → "Unresolved references"  勾上
```

配完后，你敲出 `llm.chat(Message.user(prompt))` 的**那一瞬间**就标红了。

---

## 11. 实用技巧

### `reveal_type()` —— 看 mypy 推断出了什么

```python
x = something()
reveal_type(x)          # mypy 会输出：note: Revealed type is "list[Message]"
```

**怀疑"mypy 以为这是什么类型"时，用它。**

### `# type: ignore` —— 忽略某一行

```python
import json5      # type: ignore[import-not-found]
```

**加错误码更安全**（只忽略那一种错，别的错还是会报）。

### `cast()` —— 强制断言类型

```python
from typing import cast
msg = cast(Message, x)
```

**慎用** —— 相当于对 mypy 说"相信我"，错了它就不管了。

### `TYPE_CHECKING` —— 解决循环 import

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from miniagent.core.agent import Agent   # 只在类型检查时 import，运行时跳过
```

---

## 12. 常见坑

| 坑 | 说明 |
|----|------|
| 参数没注解 = `Any` | mypy 对 `Any` **完全放弃检查** ← 最常见 |
| 以为注解运行时有用 | Python 不执行注解，只给工具看 |
| 一上来就 `--strict` | 错误太多会放弃；要渐进 |
| 不配 `ignore_missing_imports` | 第三方库刷屏 |
| 不配 `follow_imports` | 依赖内部的错误全冒出来 |
| `# type: ignore` 滥用 | 把真错误也盖住了；`warn_unused_ignores` 能提醒 |
| 把 mypy 当万能 | 它只查类型，查不出逻辑错误、边界、并发问题 |

---

## 13. 总结

```
mypy 是什么   →  不运行代码，静态检查类型
为什么需要     →  Python 类型错误只在运行时炸，还可能被 try/except 吞掉
怎么让它有用   →  ① 给参数加注解（尤其是外来依赖，用 Protocol）
                  ② 配 mypy.ini 过滤第三方噪音
                  ③ 装 PyCharm 插件 → 边写边报

最重要的一句话：
    Any = 盲区。想让 mypy 管用，就得让"外来依赖"有注解（Protocol 就是干这个的）。
```

**记住这个对比**：

```python
def summarize(messages: list[Message], llm) -> str:              # ❌ 白装 mypy
def summarize(messages: list[Message], llm: ChatModel) -> str:   # ✅ mypy 干活
```
