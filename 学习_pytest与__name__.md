# pytest 与 `__name__`：测试到底是怎么跑起来的

**适合谁**：想知道「为什么 pytest 和我直接 `python xxx.py` 结果不一样」的人。

**这份 notebook 会讲清**：

1. `__name__` 是什么，谁在改写它
2. `if __name__ == '__main__':` 到底防的是什么
3. pytest 是「导入」还是「执行」你的文件
4. 亲手写一个 20 行的 mini pytest
5. 为什么一份测试文件能同时支持 pytest 和普通运行


## 1. `__name__` 是什么？

每个 Python 模块（`.py` 文件）都有一个内置变量 `__name__`。

它的值**不是固定的**，取决于这个模块**怎么被加载**：

- **被直接执行**（`python a.py`）→ `__name__ == '__main__'`
- **被 import**（`import a`）→ `__name__ == 'a'`（模块名）

先看当前这个 notebook：

```python
print(__name__)
```

你看到 `__main__` —— 因为 Jupyter 也是「直接执行」入口代码，所以 notebook 自己就是 `__main__`。

下面做个实验，看 import 的情况。

## 2. 实验：同一个文件，两种 `__name__`

先写一个临时模块文件，里面打印自己的 `__name__`。

```python
import tempfile, textwrap
from pathlib import Path

workdir = Path(tempfile.mkdtemp())
mod_path = workdir / 'my_module.py'
mod_path.write_text(textwrap.dedent('''
    print(f'  我正在被执行，__name__ = {__name__!r}')

    def hello():
        return 'hi'
'''), encoding='utf-8')
print(mod_path.read_text(encoding='utf-8'))
```

### 2.1 直接执行它 → `__name__` 是 `'__main__'`

```python
import subprocess, sys

r = subprocess.run([sys.executable, str(mod_path)],
                   capture_output=True, text=True, encoding='utf-8')
print('【直接执行】输出：')
print(r.stdout.strip())
```

### 2.2 用 import 的方式加载它 → `__name__` 是模块名

注意：`import my_module` 会被缓存（同一个进程里只执行一次）。
为了能反复跑，这里用 `importlib` 按路径加载 —— **效果和 `import my_module` 一样**。

```python
import importlib.util

def load_module(path, name=None):
    """按路径加载一个 .py 文件。等价于 `import <模块名>`，但不会被缓存。"""
    path = Path(path)
    name = name or path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)        # ← 这一步执行顶层代码
    return mod

print('【import 方式】输出：')
my_module = load_module(mod_path)
print('拿到的模块名：', my_module.__name__)
```

**关键结论**：

| 加载方式 | `__name__` 的值 |
|---|---|
| `python my_module.py` | `'__main__'` |
| `import my_module` | `'my_module'` |

同一个文件、同一段代码，**`__name__` 的值由「谁在加载它」决定**。

## 3. `if __name__ == '__main__':` 在防什么？

它防的是：**「这段代码只在被直接执行时跑，被 import 时不要跑」**。

典型场景：一个文件既想当脚本用，又想被别的文件 import 复用里面的函数。

```python
dual_path = workdir / 'dual.py'
dual_path.write_text(textwrap.dedent('''
    def add(a, b):
        return a + b

    if __name__ == '__main__':
        print('  [脚本模式] main 块跑起来了')
        print('  1 + 2 =', add(1, 2))
'''), encoding='utf-8')
print(dual_path.read_text(encoding='utf-8'))
```

```python
# A. 直接执行 —— 会跑 if 里面的代码
r = subprocess.run([sys.executable, str(dual_path)],
                   capture_output=True, text=True, encoding='utf-8')
print('【直接执行】')
print(r.stdout)

# B. import 方式 —— 不会跑 if 里面的代码，但 add() 可以用
dual = load_module(dual_path)
print('【import 方式】add(3, 4) =', dual.add(3, 4))
print('（注意：上面没有出现「脚本模式」那段打印）')
```

**这就是为什么你的测试文件里有：**

```python
if __name__ == '__main__':
    main()
```

- **pytest 跑它** → 走「import」路径 → `main()` 不执行 → pytest 自己找 `test_*` 函数
- **你直接 `python xxx.py` 跑它** → 走「直接执行」路径 → `main()` 执行

两条路都能通，一份文件两个入口。

## 4. pytest 是怎么「发现」测试的？

pytest 的收集规则（默认）：

| 对象 | 规则 |
|---|---|
| 文件 | `test_*.py` 或 `*_test.py` |
| 函数 | `test_` 开头 |
| 类 | `Test` 开头（且不能有 `__init__`） |

**只认这些名字，其他一律不看。**

这就是为什么「只有 `main()`、没有 `test_*` 函数」的文件会让 pytest 报「空套件（空 test suite）」。

## 5. 亲手写一个 20 行的 mini pytest

理解了上面两条，你就能自己实现 pytest 的核心逻辑：

1. **import** 目标文件（不是执行！）
2. 扫描 `test_*` 函数
3. 挨个调用，`assert` 通过就 PASS，抛 `AssertionError` 就 FAIL
4. 统计

```python
import importlib.util, sys, traceback
from pathlib import Path

def mini_pytest(path):
    path = Path(path).resolve()

    # ① 导入模块（等价于 pytest 做的事）
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)          # ← 顶层代码在这里执行
    print(f'  导入时 __name__ = {mod.__name__!r}   ← 注意不是 __main__')

    # ② 收集 test_* 函数
    tests = [(n, f) for n, f in vars(mod).items()
             if n.startswith('test_') and callable(f)]
    print(f'  收集到 {len(tests)} 个测试: {[n for n, _ in tests]}')

    # ③ 逐个跑
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  PASSED  {name}')
            passed += 1
        except AssertionError:
            print(f'  FAILED  {name}')
            traceback.print_exc(limit=1)
            failed += 1

    # ④ 统计（对应真 pytest 的 "1 passed, 1 failed"）
    print(f'\n  {passed} passed, {failed} failed')
    return passed, failed
```

```python
sample = workdir / 'test_sample.py'
sample.write_text(textwrap.dedent('''
    print('[顶层代码被执行了] __name__ =', repr(__name__))

    def test_ok():
        assert 1 + 1 == 2

    def test_fail():
        assert 2 + 2 == 5, '故意失败：2+2 应该是 4'

    def not_a_test():
        print('我不是测试，pytest 不会调我')

    if __name__ == '__main__':
        print('这一行在 pytest 下不会出现')
'''), encoding='utf-8')

print('===== mini_pytest 输出 =====')
mini_pytest(sample)
```

**对照真实 pytest**，输出几乎一样：

```
collected 2 items
test_sample.py::test_ok   PASSED
test_sample.py::test_fail FAILED
========================= 1 failed, 1 passed =========================
```

你刚刚手写的 `mini_pytest` 干的就是这些事 —— **pytest 并不神秘，只是把这几步做得很完善**。

注意上面两个现象（都印证了前面讲的）：
1. `[顶层代码被执行了]` —— import 会执行顶层代码
2. 没有出现 `这一行在 pytest 下不会出现` —— `__name__` 不是 `__main__`，`if` 块被跳过

## 6. `assert` 是唯一的判据

pytest 判断「通过 / 失败」的依据只有一个：**函数里有没有抛 `AssertionError`**。

```python
def test_pass():
    assert 3 * 3 == 9
    assert 'a' in 'abc'

def test_fail_demo():
    assert 3 * 3 == 10

for f in (test_pass, test_fail_demo):
    try:
        f()
        print(f'{f.__name__}: PASSED')
    except AssertionError as e:
        print(f'{f.__name__}: FAILED -> {e!r}')
        print('   (真 pytest 还会把 "3 * 3 == 10" 这行源码也显示出来)')
```

## 7. pytest 比 mini_pytest 多了什么

| 能力 | mini_pytest | pytest |
|---|---|---|
| 导入 + 收集 + 调用 + 统计 | ✅ | ✅ |
| 只跑单个测试 | ❌ | ✅ `file.py::test_x` |
| 失败时显示源码行 + 变量值 | ❌ | ✅ |
| fixture（共享准备/清理） | ❌ | ✅ |
| 参数化（一个测试跑多组数据） | ❌ | ✅ |
| 插件 / 并行 / 覆盖率 | ❌ | ✅ |

**一句话**：`pytest = mini_pytest + 一堆工程细节`。

## 8. 回到你的项目：为什么那份测试两种都能跑

```python
def test_registry_stores_classes_not_instances(): ...   # ① pytest 认这个
def test_agent_tool_map_stores_instances(): ...
...

def main():                                              # ② 普通运行认这个
    for name, fn in ...: fn()

if __name__ == '__main__':
    main()                                               # ③ 只有直接执行才走
```

| 怎么跑 | 走哪条路 | 结果 |
|---|---|---|
| `pytest xxx.py` | ①（pytest 导入模块 → 扫 `test_*`） | 7 passed |
| `python -m miniagent.tests.test_m1_offline` | ②③（`__name__=='__main__'` → 跑 `main()`） | 7 项手写输出 |

**两条路互不干扰，两种都能用。**

## 9. 总结（背诵版）

1. `__name__` 的值由**加载方式**决定：直接执行 = `'__main__'`，import = 模块名。
2. `if __name__ == '__main__':` = 「只在被直接执行时跑这段」。
3. **pytest 是 import 你的模块**，不是执行它。所以：
   - 你的 `main()` **不会跑**
   - 顶层代码**会跑**（这点常被忽略）
   - pytest 自己扫 `test_*` 函数来调用
4. pytest 收集规则：文件 `test_*.py`，函数 `test_*`。
5. 判定标准就是有没有抛 `AssertionError`。
6. 一份文件想同时支持两种跑法：写 `test_*` 函数 + `main()` + `if __name__ == '__main__'`。

```python
# 清理临时文件
import shutil
shutil.rmtree(workdir)
print(f'已清理 {workdir}')
```
