"""P11 —— 轻量 Python 沙箱（lightweight sandbox）。

不依赖 Docker：用「子进程 + 超时 + 静态检查 + 工作目录隔离」代替容器。

【三层防护，各管各的】

    ① 静态检查（执行【前】）  源码里 import 了危险模块 / 以写入方式 open()
                            / 用绝对路径或 `..` 越界 open()
                            → 直接拒绝，代码【根本不会被执行】

    ② 超时 kill（执行【中】）  subprocess.run(timeout=N)，到点自动 kill
                            → 治死循环

    ③ 目录隔离（执行【后】）  cwd=临时目录，产出物全落在里面，父进程扫目录收走
                            → 限制写文件的位置（不是禁止写，是"只能写到这儿"）

【为什么是这三样？（和 P09 审批的分工）】

    P09 审批：问"这个操作【该不该】做"  → 需要人 → 软闸门（批准了就全权执行）
    P11 沙箱：问"做了【最多能造成多大破坏】" → 不需要人 → 硬围墙（批准了也受限）

    两者是"与"关系：批准 != 解除限制。

【它防什么、不防什么（务必诚实）】

    防：LLM 手滑 / 意外 —— 死循环、误 import os、误写文件。
    不防：恶意绕过 —— `__import__("o" + "s")`、`getattr`、
          `"".join([...])` 拼出来的名字、以及任何静态分析看不出来的花招。

    真隔离要 Docker / 容器 / 虚拟机（qwen-agent 的 code_interpreter 走的就是那条路）。
    → P11 的定位是"不装 Docker 的够用版"，不是"真安全版"。
    → 静态检查是"提高门槛"，不是"不可绕过"。别把它当安全边界宣传。

【用法】

    from miniagent.tools.sandbox import PythonSandbox

    out = PythonSandbox().call({'code': 'print(1 + 1)'})
    # out 是一个 JSON 字符串：
    #   {"status": "ok", "reason": "", "stdout": "2\\n", "stderr": "",
    #    "images": [], "elapsed": 0.05}

    sb = PythonSandbox(cfg={'timeout': 5, 'max_timeout': 20})

【返回的 status 有四种】

    ok       成功（returncode == 0）
    rejected 静态检查没通过（代码没执行）
    timeout  超时被杀
    error    子进程跑起来了，但退出码非 0（比如代码里有 bug）
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple, Union

from miniagent.core.tool import BaseTool, tool


# ============================================================================
# ① 禁名单
# ============================================================================

# 危险【模块】的「顶层名」。
# 用顶层名（alias.name.split('.')[0]）比对，这样 `import os.path` 和
# `import urllib.request` 也能被拦住 —— 只比对全名的话会漏掉一大片。
FORBIDDEN_MODULES = frozenset({
    # --- 文件系统：能读写删任意路径 ---
    'os', 'shutil', 'pathlib', 'glob', 'tempfile', 'fileinput',
    'sqlite3', 'dbm', 'shelve',
    # --- 进程 / 线程 / 系统底层 ---
    'subprocess', 'multiprocessing', 'threading', 'signal',
    'ctypes', 'pty', 'resource', 'platform', 'sys',
    # --- 网络：能外联、能当内网跳板 ---
    'socket', 'ssl', 'http', 'urllib', 'requests', 'ftplib',
    'smtplib', 'telnetlib', 'webbrowser', 'xmlrpc', 'asyncio',
    # --- 动态加载 / 反序列化：绕过静态检查的常用手段 + 直接代码执行 ---
    'importlib', 'builtins', '__builtin__', 'pickle', 'marshal',
    # --- 隐私 ---
    'getpass', 'pwd', 'grp',
})

# 危险【内建函数】。
# `__import__` 是绕过 `import` 关键字检查的头号手段，必须封。
# getattr/setattr 是"用字符串拿属性"的通用绕过通道，一并封（代价是容易误杀，
# 这里选择"宁严勿松"——沙箱是最后一道机械防线）。
FORBIDDEN_CALLS = frozenset({
    '__import__', 'eval', 'exec', 'compile',
    'globals', 'locals', 'vars',
    'getattr', 'setattr', 'delattr',
    'input', 'breakpoint', 'exit', 'quit',
})

# open() 里出现这些字符之一，就算"要写"。
_WRITE_CHARS = 'wax+'

# 回给 LLM 的输出上限（防止子进程 print 一百万行把上下文刷爆）。
_MAX_CAPTURE_CHARS = 4000

# 产出物后缀：扫这些模式收集图片路径。
_IMAGE_PATTERNS: Tuple[str, ...] = ('*.png', '*.jpg', '*.jpeg', '*.svg', '*.gif', '*.pdf')

# 哨兵：区分"没写 mode 参数"和"写了但值读不出来"。
_MISSING = object()


# ============================================================================
# ② 静态检查（用 AST，不用正则）
# ============================================================================
#
# 为什么不用正则？
#   `re.search(r'import\s+os', src)` 有两个方向的错：
#     · 误杀：`print("import os")` 里只是字符串，也被拦；
#     · 漏杀：`__import__("os")`、`import  os`（多个空格能过正则，但更花哨的写法过不了）。
#   ast.parse 先把源码变成【语法树】，注释和字符串天然不在 Import 节点里，
#   顺便还白送一个语法检查。
#
# 代价：ast 也拦不住 `__import__("o" + "s")` 这种运行时拼出来的名字 ——
# 所以第 ③ 层"超时"和第 ④ 层"目录隔离"依然必须存在，不能只靠这一层。


class SandboxRejected(Exception):
    """静态检查没通过。工具会把它转成一段说明回给 LLM，而不是抛出去炸主循环。"""


def _literal_str(node: Optional[ast.AST]) -> Optional[str]:
    """把 AST 节点还原成字符串字面量；不是字面量就返回 None。

    py3.8 之前字面量节点叫 ast.Str，3.8+ 统一成 ast.Constant。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _open_mode(call: ast.Call) -> Optional[str]:
    """取出 open(...) 的 mode 参数。

    返回：
        'r'   —— 压根没写 mode（Python 默认就是只读）
        'w'   —— 字面量，能看清楚
        None  —— 写了 mode 但它不是字面量（比如是个变量），静态看不出来
                 → 交给调用方按"拒绝"处理（fail closed，宁拒勿放）
    """
    node: Any = _MISSING
    if len(call.args) >= 2:
        node = call.args[1]                      # open(path, mode)
    else:
        for kw in call.keywords:
            if kw.arg == 'mode':                 # open(path, mode="w")
                node = kw.value
                break

    if node is _MISSING:
        return 'r'
    return _literal_str(node)


def _check_open_path(call: ast.Call) -> Optional[str]:
    """检查 open() 的路径参数，只允许【沙箱工作目录内的相对路径】。

    ★ 为什么静态也要查：`open(r'C:\\Users\\me\\.env')` 是【只读】模式，旧实现直接放行。
      可第 ③ 层"目录隔离"只管得住相对路径 —— 绝对路径会直接绕过 cwd，
      模型就能把项目里的 .env / API key 读进上下文和 trace。

    Returns:
        None  —— 通过（open() 没给路径时也放行，交给子进程报 TypeError）
        str   —— 拒绝原因
    """
    node: Any = _MISSING
    if call.args:
        node = call.args[0]                      # open(path, ...)
    else:
        for kw in call.keywords:
            if kw.arg == 'file':                 # open(file=..., ...)
                node = kw.value
                break
    if node is _MISSING:
        return None

    raw = _literal_str(node)
    if raw is None:
        # 路径是变量 / 拼接出来的 → 静态看不出会读到哪，fail closed（和 mode 同一策略）。
        return 'open() 的路径不是字符串字面量，无法确认它没有越出沙箱目录'

    # ★ 用 PureWindowsPath，不能只用 os.path.isabs：
    #   `C:\\x`、`\\\\server\\share\\x`、`\\x`、`/x` 在 Windows 上都算绝对路径，
    #   而 os.path.isabs 在 POSIX 上会放过前三种。`C:foo` 这种「盘符相对路径」
    #   也跟着盘符走，同样拒绝。
    p = PureWindowsPath(raw)
    if p.drive or p.root:
        return (f'禁止用绝对路径 open(...)：{raw!r}'
                f'（只能读写沙箱工作目录内的相对路径）')
    if '..' in p.parts:
        return f'禁止用 `..` 跳出沙箱工作目录：{raw!r}'
    return None


def _check_open_call(call: ast.Call) -> Optional[str]:
    """open(...) 系列的全部静态检查：路径不许越界 + 不许写。"""
    path_reason = _check_open_path(call)
    if path_reason is not None:
        return path_reason
    mode = _open_mode(call)
    if mode is None:
        return 'open() 的 mode 不是字面量，无法确认是只读'
    if any(ch in mode for ch in _WRITE_CHARS):
        return (f'禁止以写入方式 open(...)'
                f'（mode={mode!r}）。画图请用 plt.savefig("xx.png")')
    return None


def check_source(source: str) -> Optional[str]:
    """静态检查源码。

    Returns:
        None            —— 通过，可以执行
        str（拒绝原因）  —— 不通过，直接拒绝（原因会回给 LLM，让它自己改）
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # 白送的好处：语法错误在这里就被挡下了，不用等子进程报错。
        return f'语法错误（第 {exc.lineno} 行）: {exc.msg}'

    for node in ast.walk(tree):
        # ---------- import xxx ----------
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.')[0]
                if root in FORBIDDEN_MODULES:
                    return f'第 {node.lineno} 行：禁止 import 模块 `{alias.name}`'

        # ---------- from xxx import yyy ----------
        # ★ 很多人只检查 ast.Import，漏了 ast.ImportFrom —— `from os import path`
        #   和 `import os` 一样危险，一定要一起拦。
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or '').split('.')[0]
            if root in FORBIDDEN_MODULES:
                return f'第 {node.lineno} 行：禁止 from {node.module} import ...'

        # ---------- 函数调用 ----------
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in FORBIDDEN_CALLS:
                    return f'第 {node.lineno} 行：禁止调用 `{func.id}()`'
                if func.id == 'open':
                    reason = _check_open_call(node)
                    if reason is not None:
                        return f'第 {node.lineno} 行：{reason}'
            elif isinstance(func, ast.Attribute) and func.attr == 'open':
                # ★ io.open / codecs.open 也是"打开文件"，路径和 mode 一样要查
                #   （否则 `import io; io.open(r'C:\...\.env')` 就绕过上面那条）。
                reason = _check_open_call(node)
                if reason is not None:
                    return f'第 {node.lineno} 行：{reason}'

    return None


# ============================================================================
# ③ 执行器
# ============================================================================

def _as_text(value: Any) -> str:
    """把 TimeoutExpired.stdout 之类的东西安全地转成 str（可能是 None / bytes）。"""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _clip(text: str, limit: int = _MAX_CAPTURE_CHARS) -> str:
    """头尾各留一半，中间标出省了多少 —— 比直接砍尾巴更有信息量。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f'{text[:half]}\n... [省略 {omitted} 字符] ...\n{text[-half:]}'


def _collect_images(workdir: Path) -> List[str]:
    """扫沙箱目录，把产出物路径收集起来（返回绝对路径，调用方直接能用）。"""
    found: List[Path] = []
    for pattern in _IMAGE_PATTERNS:
        found.extend(workdir.glob(pattern))
    # 去重 + 排序，保证同样输入得到同样输出（测试好写）
    return sorted({str(p.resolve()) for p in found})


def run_code(source: str,
             *,
             timeout: int = 10,
             keep_dir: Optional[str] = None) -> Dict[str, Any]:
    """核心执行函数：静态检查 → 写临时脚本 → 子进程跑 → 收产出。

    Args:
        source:   要执行的 Python 源码
        timeout:  超时秒数，到点 kill
        keep_dir: 指定工作目录（调试时想看看沙箱目录里到底有啥就传它）；
                 不传则每次新建一个随机临时目录

    Returns:
        dict: status / reason / stdout / stderr / images / elapsed
    """
    # ---------- ① 静态检查（★ 必须在写文件之前，代码一行都不许落地） ----------
    reason = check_source(source)
    if reason is not None:
        return {
            'status': 'rejected',
            'reason': reason,
            'stdout': '',
            'stderr': '',
            'images': [],
            'elapsed': 0.0,
        }

    # ---------- ② 准备沙箱目录 ----------
    if keep_dir:
        workdir = Path(keep_dir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix='p11_sandbox_'))

    script = workdir / 'main.py'
    script.write_text(source, encoding='utf-8')

    # 子进程环境。三件事，一件都不能少：
    #   PYTHONIOENCODING   —— 强制子进程的 stdout/stderr 用 utf-8，
    #                         否则中文 Windows 上默认 cp936，输出非 GBK 字符时
    #                         子进程会抛 UnicodeEncodeError 直接挂掉。
    #   PYTHONDONTWRITEBYTECODE —— 别在沙箱目录里产 __pycache__，
    #                              否则扫产出物时会混进垃圾。
    #   MPLBACKEND=Agg     —— 兜底：LLM 忘了写 matplotlib.use('Agg') 也能出图，
    #                         而不是因为"无显示器"报错。
    env = dict(os.environ)
    env.update({
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'MPLBACKEND': 'Agg',
    })

    # ---------- ③ 子进程执行 ----------
    t0 = time.time()
    try:
        proc = subprocess.run(
            # ★ -u = unbuffered。没有它，子进程的 print 会先攒在缓冲区里，
            #   而我们 timeout 时是直接 kill —— 缓冲区里的内容就【全丢了】。
            #   于是「超时前跑到哪一步」这个最该拿到的信息，反而是空的。
            #   （这就是 subprocess 文档第 11 章那个 -u 坑的现场复现。）
            [sys.executable, '-u', '-X', 'utf8', str(script)],
            cwd=str(workdir),      # ★ 子进程里所有相对路径都落在沙箱目录
            capture_output=True,   # ★ 把 stdout/stderr 接管道收回来
            encoding='utf-8',      # ★ 和 PYTHONIOENCODING 对齐
            errors='replace',      # ★ 编码对不上时只替换，绝不让读取线程默默死掉
                                   #    （不写它 → 解码失败会让 stdout 静默变成 None）
            timeout=timeout,       # ★ 到点自动 kill —— 治死循环
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # run() 在抛这个异常之前，已经先把子进程 kill 掉并 wait 完了。
        return {
            'status': 'timeout',
            'reason': f'执行超过 {timeout} 秒已强制终止（可能是死循环）',
            'stdout': _clip(_as_text(exc.stdout)),
            'stderr': _clip(_as_text(exc.stderr)),
            'images': _collect_images(workdir),
            'elapsed': round(time.time() - t0, 3),
        }

    elapsed = round(time.time() - t0, 3)

    # ---------- ④ 判断结果 + 收产出 ----------
    ok = proc.returncode == 0
    return {
        'status': 'ok' if ok else 'error',
        'reason': '' if ok else f'子进程退出码 {proc.returncode}',
        'stdout': _clip(proc.stdout or ''),
        'stderr': _clip(proc.stderr or ''),
        'images': _collect_images(workdir),
        'elapsed': elapsed,
    }


# ============================================================================
# ④ 工具（给 Agent 用）
# ============================================================================

@tool('python_sandbox')
class PythonSandbox(BaseTool):
    """在受限子进程里执行 Python 代码（轻量、无 Docker）。"""

    name = 'python_sandbox'
    description = (
        '在受限子进程中执行一段 Python 代码，适合画图与计算。'
        '限制：禁止 import os/subprocess/socket 等危险模块；'
        'open() 只允许沙箱工作目录内的相对路径（禁止绝对路径、禁止 `..` 越界）；'
        '禁止以写入方式 open()'
        '（画图请用 plt.savefig("xxx.png")，图片会被自动收集并返回路径）；'
        '超过超时时间会被强制终止。返回 JSON 字符串。'
    )
    parameters: Dict[str, Any] = {
        'type': 'object',
        'properties': {
            'code': {
                'type': 'string',
                'description': '要执行的 Python 源码',
            },
            'timeout': {
                'type': 'integer',
                'description': '超时秒数（默认 10，实际会被夹到 cfg.max_timeout 以内）',
            },
        },
        'required': ['code'],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)
        # 默认超时；可用 cfg={'timeout': 5} 覆盖
        self.default_timeout = int(self.cfg.get('timeout', 10))
        # ★ 上限。LLM 是可以通过参数传 timeout 的 ——
        #   如果不封顶，它传个 timeout=99999 就把"超时保护"自己拆了。
        #   凡是【LLM 能控制的参数】，都要在服务端夹到安全区间。
        self.max_timeout = int(self.cfg.get('max_timeout', 30))
        # 传了就用固定目录（调试时想进去看产出物很方便）
        self.workdir: Optional[str] = self.cfg.get('workdir')

    # ------------------------------------------------------------------
    def call(self, params: Union[str, dict], **kwargs) -> str:
        """执行代码。params 是 {'code': ..., 'timeout': ...}（dict 或 JSON 字符串）。

        约定：永远返回字符串，永远不抛异常。
        失败信息也当成正常结果回给 LLM —— 让它自己看报错改代码，这是 Agent 的自我修复能力。
        """
        # 解析参数也可能失败（LLM 偶尔会吐出半个 JSON），
        # 所以这里也要兜住 —— 工具抛异常 = Agent 主循环被炸。
        try:
            data = self.parse_params(params)
        except (ValueError, TypeError) as exc:      # json.JSONDecodeError 是 ValueError 的子类
            return json.dumps(
                {'status': 'rejected', 'reason': f'参数不是合法 JSON: {exc}', 'images': []},
                ensure_ascii=False,
            )

        if not isinstance(data, dict):
            return json.dumps(
                {'status': 'rejected', 'reason': '参数必须是一个对象', 'images': []},
                ensure_ascii=False,
            )

        code = (data.get('code') or '').strip()
        if not code:
            return json.dumps(
                {'status': 'rejected', 'reason': 'code 为空', 'images': []},
                ensure_ascii=False,
            )

        timeout = self._resolve_timeout(data.get('timeout'))
        result = run_code(code, timeout=timeout, keep_dir=self.workdir)
        return json.dumps(result, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    def _resolve_timeout(self, requested: Any) -> int:
        """把 LLM 给的 timeout「夹」到 [1, max_timeout] 区间里。

        三种输入都要能接住：
            None / 没传 / 'abc'  → 用默认值
            -5                   → 抬到 1
            99999                → 压到 max_timeout
        """
        try:
            value = int(requested)
        except (TypeError, ValueError):
            return self.default_timeout
        return max(1, min(value, self.max_timeout))


# ============================================================================
# 自测：不用 pytest，直接跑这个文件也行
# ============================================================================

if __name__ == '__main__':
    sb = PythonSandbox()

    PLOT_CODE = (
        'import matplotlib\n'
        'matplotlib.use("Agg")\n'
        'import matplotlib.pyplot as plt\n'
        'plt.plot([1, 2, 3], [1, 4, 9])\n'
        'plt.title("y = x^2")\n'
        'plt.savefig("plot.png", dpi=80)\n'
        'print("saved")\n'
    )

    def show(title: str, code: str, **extra) -> None:
        print(f'\n=== {title} ===')
        print(sb.call({'code': code, **extra}))

    show('1. 正常计算', 'print("1+1 =", 1 + 1)')
    show('2. 画图 -> 产出 PNG', PLOT_CODE)
    show('3. import os -> 拒绝', 'import os\nprint(os.listdir("."))')
    show('4. __import__ 绕过 -> 也拒绝', 'm = __import__("os")\nprint(m.getcwd())')
    show('5. open(..., "w") -> 拒绝', 'open("evil.txt", "w").write("x")')
    show('6. 字符串里的 import 不误杀', 'print("import os")')
    show('7. 死循环 -> 超时 kill', 'while True:\n    pass\n', timeout=1)
    show('8. 代码有 bug -> status=error', 'raise ValueError("boom")')
