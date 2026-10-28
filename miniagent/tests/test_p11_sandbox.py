"""test_p11_sandbox.py —— P11 验收测试（全离线：只跑子进程，不联网、不阻塞）。

跑法：
    .venv\\Scripts\\python.exe -m pytest miniagent/tests/test_p11_sandbox.py -v
    .venv\\Scripts\\python.exe -m miniagent.tests.test_p11_sandbox

DAILY_PLAN 的三条验收：
    [x] 正常画图代码能产出 PNG
    [x] import os / subprocess / socket ... 被拒（且拒绝发生在执行【前】）
    [x] 死循环被 kill

额外补的回归（都是踩过坑的）：
    [+] 静态检查用 AST，所以字符串/注释里的 "import os" 不该误杀
    [+] `from os import path` 必须和 `import os` 一样被拦
    [+] `__import__("os")` 必须被拦（绕过 import 关键字的老套路）
    [+] open 的 mode 是变量 → 静态看不出 → 宁拒勿放
    [+] LLM 传 timeout=99999 必须被夹到 max_timeout
    [+] 大量输出不能把管道撑死（subprocess 的经典死锁）
    [+] 产出物必须落在沙箱目录，不能污染项目目录
    [+] call() 只返回字符串，失败也不抛异常（否则会炸掉 Agent 主循环）
"""
from __future__ import annotations

import json
import os
import pytest
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from miniagent.core.tool import TOOL_REGISTRY  # noqa: E402
from miniagent.tools.sandbox import (  # noqa: E402
    PythonSandbox,
    check_source,
    run_code,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# 样例代码
# ============================================================================

PLOT_CODE = (
    'import matplotlib\n'
    'matplotlib.use("Agg")\n'
    'import matplotlib.pyplot as plt\n'
    'plt.plot([1, 2, 3], [1, 4, 9])\n'
    'plt.title("y = x^2")\n'
    'plt.savefig("p11_plot.png", dpi=80)\n'
    'print("saved")\n'
)

# 静态检查【应该拒绝】的片段
SHOULD_REJECT = [
    'import os',
    'import os.path',                       # 用顶层名比对，这个也要拦
    'from os import path',                  # ast.ImportFrom 是最常被漏掉的一个
    'from os.path import join',
    'import subprocess',
    'import socket',
    'import sys',
    'import shutil',
    'import urllib.request',
    'import requests',
    'import ctypes',
    'import importlib',
    'import pickle',
    '__import__("os")',                     # 绕过 import 关键字
    'eval("1 + 1")',
    'exec("x = 1")',
    'compile("pass", "<s>", "exec")',
    'getattr(1, "real")',
    'open("a.txt", "w")',
    'open("a.txt", "a")',
    'open("a.txt", "wb")',
    'open("a.txt", mode="x")',
    'open("a.txt", "r+")',
    'm = "w"\nopen("a.txt", m)',            # mode 是变量 → 看不出来 → 拒
]

# 静态检查【应该放行】的片段
SHOULD_ALLOW = [
    'print(1 + 1)',
    'print("import os")',                   # ★ 字符串里的，不是真 import
    '# import os\nprint(1)',                # ★ 注释里的
    'import numpy as np',                   # 画图要用的，不能误杀
    'import pandas as pd',
    'import matplotlib.pyplot as plt',
    'from math import sqrt',
    'open("a.txt")',                        # 默认只读模式
    'open("a.txt", "r")',
    'open("a.txt", encoding="utf-8")',
    'x = [i ** 2 for i in range(10)]',
]


# ============================================================================
# 一、注册与 schema
# ============================================================================

def test_registered_in_global_registry():
    assert 'python_sandbox' in TOOL_REGISTRY, '没注册进 TOOL_REGISTRY'
    assert TOOL_REGISTRY['python_sandbox'] is PythonSandbox


def test_schema_is_llm_friendly():
    tool_obj = PythonSandbox()
    schema = tool_obj.schema
    assert schema['name'] == 'python_sandbox'
    assert schema['description'], '描述不能为空，LLM 靠它决定要不要调'
    assert 'code' in schema['parameters']['properties']
    assert schema['parameters']['required'] == ['code']


# ============================================================================
# 二、静态检查（第 ① 层防护）
# ============================================================================

def test_dangerous_snippets_are_rejected():
    """★ 验收：import os 等被拒。"""
    for bad in SHOULD_REJECT:
        reason = check_source(bad)
        assert reason is not None, f'居然放过了: {bad!r}'


def test_safe_snippets_are_allowed():
    """反向验收：不能误杀。"""
    for good in SHOULD_ALLOW:
        reason = check_source(good)
        assert reason is None, f'误杀了 {good!r} -> {reason}'


def test_ast_not_regex_string_and_comment_are_safe():
    """★ 这条是"为什么用 ast 而不是正则"的直接证据。"""
    assert check_source('print("import os")') is None
    assert check_source('# import os\nprint(1)') is None
    assert check_source('msg = "subprocess is dangerous"') is None


def test_import_from_is_caught():
    """★ 回归：只查 ast.Import 会漏掉 `from os import path`。"""
    assert check_source('from os import path') is not None
    assert check_source('from subprocess import run') is not None


def test_syntax_error_rejected():
    assert check_source('def f(:\n    pass') is not None


# ============================================================================
# 二·补、open() 路径限制（审计报告 I 模块的洞）
# ============================================================================
#
# 旧实现只拦「写」open。`open(r'C:\...\.env')` 是只读模式，直接放行 ——
# 模型可以把项目里的 .env / API key 读进上下文和 trace。
# 修法：静态检查要求 open 的路径必须是【沙箱目录内的相对路径】。

ABSOLUTE_OPEN_SNIPPETS = [
    r'open(r"C:\Users\me\.env")',
    'open("C:/Users/me/.env")',
    r'open(r"\\server\share\secret.txt")',       # UNC 路径
    r'open(r"\Windows\win.ini")',                # 盘根相对
    'open("/etc/passwd")',                       # POSIX 绝对路径，Windows 上也会落到盘根
    r'open(file=r"C:\Users\me\.env")',           # 关键字传参
]


def test_absolute_open_path_is_rejected():
    """★ 回归：只读 open 也不能用绝对路径读沙箱外的文件（比如 .env）。"""
    for snippet in ABSOLUTE_OPEN_SNIPPETS:
        reason = check_source(snippet)
        assert reason is not None, f'绝对路径 open 被放行了: {snippet!r}'
        assert '绝对路径' in reason, f'拒绝原因说不清问题: {reason!r}'


def test_parent_dir_traversal_open_path_is_rejected():
    """`..` 能从 cwd 跳出沙箱目录，同样拒绝。"""
    for snippet in ['open("../secret.txt")',
                    'open("a/../../b.txt")',
                    r'open(r"..\secret.txt")',
                    'open("..")']:
        reason = check_source(snippet)
        assert reason is not None, f'越界路径被放行了: {snippet!r}'
        assert '..' in reason or '跳' in reason, reason


def test_computed_open_path_is_rejected():
    """路径不是字面量（变量 / 拼接）→ 静态看不出读到哪 → 宁拒勿放。"""
    assert check_source('p = "a" + ".txt"\nopen(p)') is not None


def test_attribute_open_is_also_checked():
    """`io.open` 是另一条文件打开通道，不能只查裸名字的 open。"""
    assert check_source('import io\nio.open(r"C:\\Users\\me\\.env")') is not None
    assert check_source('import io\nio.open("a.txt", "w")') is not None
    assert check_source('import io\nio.open("a.txt")') is None


def test_relative_open_path_still_allowed():
    """正常相对路径（沙箱目录内）不能误杀。"""
    assert check_source('open("a.txt")') is None
    assert check_source('open("sub/dir/a.txt")') is None
    assert check_source('open(r"sub\\a.txt", "r", encoding="utf-8")') is None


def test_absolute_read_end_to_end_cannot_leak_secret(tmp_path):
    """★ 端到端：沙箱里读沙箱外的 secret 文件 → 静态拒绝，内容不进 stdout。"""
    secret = tmp_path / '.env'
    secret.write_text('API_KEY=sk-should-never-leak', encoding='utf-8')
    code = f'print(open({str(secret)!r}).read())'

    out = run_code(code)                     # 不传 keep_dir：workdir 和 secret 完全无关
    assert out['status'] == 'rejected', out
    assert 'sk-should-never-leak' not in out['stdout']
    assert 'sk-should-never-leak' not in out['reason']


def test_relative_read_inside_workdir_still_works(tmp_path):
    """反向验收：沙箱目录内的相对路径只读 open 必须还能用。"""
    (tmp_path / 'data.txt').write_text('hello sandbox', encoding='utf-8')

    out = run_code('print(open("data.txt", encoding="utf-8").read())',
                   keep_dir=str(tmp_path))
    assert out['status'] == 'ok', out
    assert 'hello sandbox' in out['stdout']



def test_rejected_code_never_creates_script(tmp_path):
    """★★ 拒绝必须发生在【写文件之前】—— 连临时脚本都不该落地。"""
    out = run_code('import os', keep_dir=str(tmp_path))
    assert out['status'] == 'rejected'
    assert list(tmp_path.iterdir()) == [], '被拒绝的代码居然留了文件在沙箱目录'


def test_rejected_result_has_empty_outputs():
    out = json.loads(PythonSandbox().call({'code': 'import os'}))
    assert out['status'] == 'rejected'
    assert out['stdout'] == '' and out['stderr'] == ''
    assert out['images'] == []
    assert 'os' in out['reason'] or 'import' in out['reason']


# ============================================================================
# 三、验收 1：能跑、能画图、能产出 PNG
# ============================================================================

def test_plain_code_runs():
    raw = PythonSandbox().call({'code': 'print(1 + 1)'})
    assert isinstance(raw, str), 'call() 必须返回字符串'
    out = json.loads(raw)
    assert out['status'] == 'ok', out
    assert out['stdout'].strip() == '2'


def test_accepts_json_string_params():
    """LLM 传过来的 params 是 JSON 字符串，不是 dict。"""
    out = json.loads(PythonSandbox().call('{"code": "print(7)"}'))
    assert out['status'] == 'ok'
    assert out['stdout'].strip() == '7'


def test_plotting_produces_png():
    """★ 验收：正常画图代码能产出 PNG。"""
    pytest.importorskip('matplotlib', reason='产图测试需要 matplotlib，未安装则跳过')
    out = json.loads(PythonSandbox().call({'code': PLOT_CODE}))
    assert out['status'] == 'ok', out
    assert len(out['images']) == 1, f'应该正好一张图: {out["images"]}'
    image = out['images'][0]
    assert image.endswith('p11_plot.png')
    assert os.path.getsize(image) > 0, '图片是空文件'


def test_produced_file_is_real_png():
    """产出物真是 PNG（看魔数），不是改了个后缀的空文件。"""
    pytest.importorskip('matplotlib', reason='产图测试需要 matplotlib，未安装则跳过')
    out = json.loads(PythonSandbox().call({'code': PLOT_CODE}))
    with open(out['images'][0], 'rb') as fp:
        assert fp.read(8) == b'\x89PNG\r\n\x1a\n', '不是合法 PNG'


def test_output_stays_inside_sandbox():
    """★ 目录隔离：产出物落在沙箱目录，不污染项目目录。"""
    pytest.importorskip('matplotlib', reason='产图测试需要 matplotlib，未安装则跳过')
    out = json.loads(PythonSandbox().call({'code': PLOT_CODE}))
    image = out['images'][0]
    assert not image.startswith(REPO_ROOT), f'产出物漏进了项目目录: {image}'
    assert not os.path.exists(os.path.join(REPO_ROOT, 'p11_plot.png'))


def test_plot_works_even_without_agg(monkeypatch=None):
    """环境变量 MPLBACKEND=Agg 兜底：LLM 忘了 use('Agg') 也能出图。"""
    pytest.importorskip('matplotlib', reason='产图测试需要 matplotlib，未安装则跳过')
    code = (
        'import matplotlib.pyplot as plt\n'
        'plt.plot([1, 2], [1, 2])\n'
        'plt.savefig("no_agg.png")\n'
    )
    out = json.loads(PythonSandbox().call({'code': code}))
    assert out['status'] == 'ok', out
    assert any(p.endswith('no_agg.png') for p in out['images'])


# ============================================================================
# 四、验收 2：死循环被 kill（第 ② 层防护）
# ============================================================================

def test_infinite_loop_is_killed():
    """★ 验收：死循环被 kill，而且不能等太久。"""
    t0 = time.time()
    out = json.loads(PythonSandbox().call({'code': 'while True:\n    pass\n', 'timeout': 1}))
    cost = time.time() - t0

    assert out['status'] == 'timeout', out
    assert cost < 8, f'过了 {cost:.1f}s 还没被杀掉'
    assert '超' in out['reason'] or 'timeout' in out['reason'].lower()


def test_timeout_output_before_kill_is_kept():
    """超时前已经 print 的内容要留下来（告诉 LLM 跑到哪一步了）。"""
    code = 'print("step 1")\nwhile True:\n    pass\n'
    out = json.loads(PythonSandbox().call({'code': code, 'timeout': 1}))
    assert out['status'] == 'timeout'
    assert 'step 1' in out['stdout']


def test_sleep_beyond_timeout_also_killed():
    """不只是死循环 —— 单纯睡得久也要被杀。"""
    out = json.loads(PythonSandbox().call({'code': 'import time\ntime.sleep(30)', 'timeout': 1}))
    assert out['status'] == 'timeout', out


def test_cfg_timeout_is_used_when_llm_gives_none():
    sb = PythonSandbox(cfg={'timeout': 1})
    t0 = time.time()
    out = json.loads(sb.call({'code': 'while True: pass'}))
    assert out['status'] == 'timeout'
    assert time.time() - t0 < 8


def test_llm_cannot_raise_timeout_above_max():
    """★ 安全回归：LLM 传 timeout=99999 必须被夹到 max_timeout。

    否则它等于自己把"超时这道门"拆了。
    """
    sb = PythonSandbox(cfg={'timeout': 2, 'max_timeout': 2})
    assert sb._resolve_timeout(99999) == 2
    assert sb._resolve_timeout(-5) == 1
    assert sb._resolve_timeout(0) == 1
    assert sb._resolve_timeout(None) == 2
    assert sb._resolve_timeout('abc') == 2
    assert sb._resolve_timeout('7') == 2      # 字符串能转 int，但也受 max 限制


def test_llm_timeout_cannot_exceed_30_by_default():
    sb = PythonSandbox()
    assert sb._resolve_timeout(10_000) == sb.max_timeout


# ============================================================================
# 五、其它行为
# ============================================================================

def test_code_error_reports_stderr():
    """子进程跑起来了但挂了 → status=error，stderr 里有报错，交给 LLM 改。"""
    out = json.loads(PythonSandbox().call({'code': 'raise ValueError("boom")'}))
    assert out['status'] == 'error', out
    assert 'ValueError: boom' in out['stderr']
    assert 'ValueError: boom' in out['reason'] or out['reason'].startswith('子进程退出码')


def test_empty_code_rejected():
    out = json.loads(PythonSandbox().call({'code': '   \n  '}))
    assert out['status'] == 'rejected'


def test_large_output_does_not_deadlock_and_is_clipped():
    """★ subprocess 经典死锁回归 + 上下文保护。

    输出量远超管道缓冲区（64KB）时，run() 内部用 communicate() 所以不会死锁；
    同时回给 LLM 的文本会被裁剪，不会把上下文刷爆。
    """
    code = 'for i in range(20000):\n    print(i)\n'
    out = json.loads(PythonSandbox().call({'code': code}))
    assert out['status'] == 'ok', out
    assert '省略' in out['stdout'], '输出没被裁剪，可能把上下文刷爆'


def test_non_ascii_output_survives():
    """★ 编码回归：中文输出不能因为 cp936/utf-8 打架而变成 None。"""
    out = json.loads(PythonSandbox().call({'code': 'print("中文输出测试")'}))
    assert out['status'] == 'ok', out
    assert '中文输出测试' in out['stdout']


def test_call_never_raises_on_weird_params():
    """call() 永远不能抛异常 —— 抛了就会炸掉 Agent 主循环。"""
    sb = PythonSandbox()
    for weird in [None, '', '{}', {'code': None}, {}, 'not json at all']:
        result = sb.call(weird)          # 不该抛
        assert isinstance(result, str), f'{weird!r} 没有返回字符串'


def test_dangerous_import_actually_denied_end_to_end():
    """端到端：走一遍 call()，确认 os 真的没被执行。"""
    out = json.loads(PythonSandbox().call({
        'code': 'import os\nprint("这个不该打印")\nprint(os.listdir("."))'
    }))
    assert out['status'] == 'rejected'
    assert '这个不该打印' not in out['stdout']


# ============================================================================
# 脚本模式入口
# ============================================================================

def main() -> None:
    import tempfile

    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if 'tmp_path' in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(__import__('pathlib').Path(d))
            else:
                fn()
            print(f'  [ok]   {name}')
            passed += 1
        except Exception as exc:
            print(f'  [FAIL] {name}  ->  {type(exc).__name__}: {exc}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


if __name__ == '__main__':
    main()
