"""P10 —— 文件工具：能【声明】自己会碰哪些路径，好让 Checkpoint 先存档。

【这两个工具是回滚演示的"文件世界"来源】
    回滚要能恢复文件，前提是"改之前先存档"。谁最清楚"这次会改哪个文件"？
    当然是工具自己 —— 参数里的 path 就是答案。

    所以工具多实现一个方法（BaseTool.declare_writes），只【声明】不改行为；
    真正调 snapshot_file 的是 Agent 的主循环（core/mem_agent.py）。

【root 从哪来】
    工具是 Agent 按名字实例化的，配置一路传进来：
        Agent(tools=[{'name': 'write_file', 'root': workdir}])
                                              ↑ 这就是 cfg
    不传 root → 退化成当前工作目录（cwd）。
    为什么一定要 root：Checkpoint 只能回滚它 root 之内的文件
    （越界会抛 ValueError），所以存档目录必须和工具的工作目录对齐。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from miniagent.core.tool import BaseTool, tool


class _FileTool(BaseTool):
    """文件类工具的公共部分：解析 root、把相对路径拼成绝对路径。"""

    def __init__(self, cfg: dict = None):
        super().__init__(cfg)
        self.root = Path(self.cfg.get('root', '.')).resolve()

    def _path(self, rel: str) -> Path:
        """相对路径 → 绝对路径（统一在 root 下；越界直接拒绝）。

        ★ pathlib 的 `/` 运算符遇到【绝对路径】会吞掉左边的 root：
              Path('root') / 'C:\\outside.txt'  ==  'C:\\outside.txt'
          所以不能直接 `return (self.root / rel).resolve()` —— 必须先拼、
          再 resolve，最后校验结果仍在 root 之内。`..` 同理（resolve 后会露出真实位置）。
        """
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f'路径越界：{rel!r} 不在工作目录 {self.root} 内')
        return p


@tool('read_file')
class ReadFileTool(_FileTool):
    """读文件。它【不改】任何东西，所以 declare_writes 保持默认（空列表）。"""

    description = '读取一个文本文件的内容。路径相对于工作目录。'
    parameters = {
        'type': 'object',
        'properties': {'path': {'type': 'string', 'description': '文件路径，如 hello.txt'}},
        'required': ['path'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        f = self._path(p['path'])
        if not f.is_file():
            return f'错误：文件不存在 {f}'
        return f.read_text(encoding='utf-8')


@tool('write_file')
class WriteFileTool(_FileTool):
    """写文件。★ declare_writes 把要写的路径报给 Agent，Agent 会在动手前存档。"""

    description = '把内容写入一个文本文件（覆盖写；文件不存在则新建）。'
    parameters = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '文件路径，如 hello.txt'},
            'content': {'type': 'string', 'description': '要写入的完整内容'},
        },
        'required': ['path', 'content'],
    }

    def declare_writes(self, params: Union[str, dict]) -> list:
        """★ 只声明，不动手：这次会碰哪个文件。

        解析失败（例如模型给了半截 JSON）就返回空列表 —— 声明失败顶多是
        "少存了一次档"，绝不能让"存档"这件事把主循环打断。
        """
        try:
            p = self.parse_params(params)
        except Exception:                                   # noqa: BLE001
            return []
        rel = p.get('path')
        if not rel:
            return []
        try:
            return [self._path(rel)]
        except ValueError:
            # 越界路径不声明（root 外的文件本来也存不了档），真正的拒绝由 call() 抛。
            return []

    def call(self, params: Union[str, dict], **kwargs) -> str:
        p = self.parse_params(params)
        f = self._path(p['path'])
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(p['content'], encoding='utf-8')
        print(f'  [write_file] {f} = {p["content"]!r}')
        return f'已写入 {f}'
