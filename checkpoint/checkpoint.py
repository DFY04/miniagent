"""Checkpoint：用同一个 step 编号，把「上下文」和「外部文件」两个世界同步回滚。

【要解决的问题】
    Agent 跑任务时，会同时改变两个世界：
      1. 上下文世界 —— messages 列表不断 append assistant / function 消息
      2. 文件世界   —— 工具把磁盘上的文件改掉（写、删、建）

    上一版 AgentCheckpoint 只能回滚「文件世界」：磁盘文件是恢复了，但上下文
    还停在"这件事已经做过"的状态，模型会以为办完了，接着往下推理 ——
    两个世界就此错位。

【核心思想：step 是连接两个世界的钥匙】
    我们给两个世界里的每一样东西都盖上同一个 step 编号：

        上下文世界：Message.step            （上一讲加的）
        文件世界  ：file_ops.step / tool_calls.step （sqlite 表）

    于是「回滚到 step N 之前」就被翻译成两句对称的话：

        上下文：删掉所有 step >= N 的消息
        文件  ：把 step >= N 的文件操作恢复掉

    一句话：两个世界共用一个时钟，回滚时把两个世界都拨回同一个时刻。

【为什么 step 从 0 开始】
    要和 Agent 主循环 `for step in range(max_steps)`、以及 Message.step 对齐。
    注意：你贴的那个 AgentCheckpoint 例子是 1 起（`COALESCE(MAX(step), 0) + 1`），
    这里改成 0 起（`COALESCE(MAX(step), -1) + 1`），否则两个世界会差 1。

【方案 A：step 只有一个真相来源（本文件采用）】
    Checkpoint 不再自己"数"步数（原来 log_tool_call 里有 self.step += 1），
    而是由 agent 每轮循环开头调用 begin_step(step)，把【循环变量】照进来：

        for step in range(...):
            ckpt.begin_step(step)     ← 唯一时钟来源
            reply.step = step         ← 上下文用同一个 step
            Message.function(..., step=step)

    这样上下文和文件用的是同一个变量，不再靠"每轮恰好调一次 log_tool_call"的约定。
    好处：万一某轮漏调了 log_tool_call，下一轮 begin_step 会把步号纠回来（自愈）。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


class Checkpoint:
    """操作前存档 / 出错后「上下文 + 文件」一起回滚。

    用法（方案 A：step 由 agent 的循环变量显式驱动）：
        ckpt = Checkpoint(root, messages)   # messages 必须是 agent 正在用的那个 list 对象

        start = ckpt.step                   # 新跑=0；回滚/重启后=恢复点
        for step in range(start, start + max_steps):
            ckpt.begin_step(step)                   # ★ 唯一时钟：把循环变量照进来
            reply = llm.chat(messages)
            reply.step = step                       # 上下文世界：盖上【同一个】步号
            messages.append(reply)
            ...
            ckpt.snapshot_file(path)                # 文件世界：改之前存档
            observation = tool(...)
            ckpt.log_tool_call(name, args, observation)   # 只记账，不推进时钟
            messages.append(Message.function(..., step=step))   # 又一个【同一个】步号

        ckpt.rollback(1)    # 两个世界一起回到 step 1 之前
    """

    def __init__(self, root: str, messages: List[Any]):
        self.root = Path(root).resolve()
        self.messages = messages                # ★ 保存 list 的引用（回滚时原地截断它）
        self.meta_dir = self.root / ".agent"
        self.shadow_dir = self.meta_dir / "checkpoints"
        self.log_db = self.meta_dir / "checkpoints.db"

        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.step = self._get_latest_step()     # 0 起：空库时是 0

    # ============================================================
    #  基础设施
    # ============================================================

    def _connect(self):
        conn = sqlite3.connect(self.log_db, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    step   INTEGER PRIMARY KEY,
                    tool   TEXT NOT NULL,
                    input  TEXT,
                    output TEXT,
                    ts     TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_ops (
                    step     INTEGER NOT NULL,
                    op       TEXT    NOT NULL,   -- 'modified' | 'created'
                    rel_path TEXT    NOT NULL,   -- 相对 root 的路径
                    PRIMARY KEY (step, rel_path)
                );
            """)

    def _get_latest_step(self) -> int:
        """恢复上次进度：下一步 = 已记录的最大 step + 1。

        为什么用 COALESCE(MAX(step), -1) 而不是 0：
            0 起的话，空库要返回 0，而不是 1。
            空库 MAX 是 NULL → COALESCE 成 -1 → +1 = 0。✓
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(step), -1) FROM tool_calls"
            ).fetchone()
        return row[0] + 1

    def bind(self, messages: List[Any]) -> None:
        """★ 把回滚目标换成另一份消息列表（原地回滚会改它）。

        【为什么需要这个方法】
            Checkpoint 是在构造时抓住 list 的【引用】的，回滚时 `self.messages[:] = kept`
            原地截断的就是那个 list。但 Agent 每轮 run(messages) 收到的 list 可能不同：
              - 有 mem 时 → 上下文是 mem.message（构造时就绑好了）
              - 没 mem 时 → 上下文是 run() 传进来的那个 list（构造时还不知道是谁）
            所以 run() 一开头就调一次 bind()，保证「回滚的那份」和「模型正在读的那份」
            永远是同一个对象。
        """
        self.messages = messages

    def _rel(self, file_path) -> Path:
        """任意路径 → 相对 root 的路径；越界报错。"""
        p = Path(file_path).resolve()
        try:
            return p.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"{p} 不在 root ({self.root}) 内") from exc

    def _abs(self, rel_path) -> Path:
        """相对路径 → 绝对路径（给人看的，日志/打印都用绝对路径）。"""
        return (self.root / Path(rel_path)).resolve()

    # ============================================================
    #  文件世界：改之前存档
    # ============================================================

    def snapshot_file(self, file_path) -> Path:
        """在「动手修改」之前调用。

        - 文件已存在 → 复制到 shadow_dir/step-XXXX/<相对路径>，记为 'modified'
        - 文件不存在 → 不复制，记为 'created'（回滚时删掉即可）

        返回相对 root 的 Path。
        """
        rel = self._rel(file_path)
        src = self.root / rel

        if src.is_file():
            dest = self.shadow_dir / f"step-{self.step:04d}" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            op = "modified"
        else:
            op = "created"

        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_ops (step, op, rel_path) "
                "VALUES (?, ?, ?)",
                (self.step, op, str(rel)),
            )

        print(f"[snapshot] step-{self.step:04d}  {op:<8} {src}")
        return rel

    # ============================================================
    #  唯一时钟：step 由 agent 的循环变量驱动（方案 A）
    # ============================================================

    def begin_step(self, step: int) -> None:
        """★ agent 每轮循环开头调用：把「现在第几步」显式照进来。

        这是【唯一】的时钟来源——Checkpoint 不再自己数步数，只是镜像
        agent 的循环变量，所以上下文和文件的编号不可能错开。

        必须在【这一轮任何 snapshot_file 之前】调用：
            snapshot_file 用 self.step 决定存到哪个 step-XXXX 目录，
            照晚了就会把这一轮的文件存进上一轮的目录。

        自愈：就算上一轮漏调了 log_tool_call，这里也会把步号直接设成正确值，
        不会像"自己 += 1"那样越错越远。
        """
        self.step = step

    # ============================================================
    #  文件世界：记录这一步做了什么（只记账，不推进时钟）
    # ============================================================

    def log_tool_call(self, tool: str, tool_input: str, output: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls (step, tool, input, output, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.step, tool, tool_input, output,
                 datetime.now().isoformat(timespec="seconds")),
            )
        # ★ 方案 A：这里【没有】 self.step += 1
        #   时钟由 begin_step 推进，记账只负责"记"，不负责"走"。

    # ============================================================
    #  上下文世界：回滚
    # ============================================================

    def _rollback_context(self, target_step: int):
        """删掉上下文里所有 step >= target_step 的消息（原地改 list）。

        关键点：system / user 消息的 step 是 None（不在循环里产生），
        它们永远保留。这样回滚后模型还保有「你是谁 + 用户问了什么」。
        """
        kept = [m for m in self.messages
                if (m.step is None) or (m.step < target_step)]
        self.messages[:] = kept          # ★ 原地替换，外部拿到的还是同一个 list
        print(f"[rollback] 上下文截断到 {len(kept)} 条消息")
        return self.messages

    # ============================================================
    #  文件世界：回滚
    # ============================================================

    def _rollback_files(self, target_step: int):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT step, op, rel_path FROM file_ops "
                "WHERE step >= ? ORDER BY step ASC",
                (target_step,),
            ).fetchall()

        # 同一文件只保留最早那条（后面几步对同一文件的操作，回滚一次就够）
        earliest: Dict[Path, tuple] = {}
        for s, op, rel in rows:
            earliest.setdefault(Path(rel), (s, op))

        for rel, (s, op) in earliest.items():
            dest = self.root / rel
            if op == "created":
                if dest.exists():
                    dest.unlink()
                    try:
                        dest.parent.rmdir()
                    except OSError:
                        pass
                print(f"[rollback] step-{s:04d}  remove   {dest}")
            else:
                snap = self.shadow_dir / f"step-{s:04d}" / rel
                if snap.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap, dest)
                    print(f"[rollback] step-{s:04d}  restore  {dest}  ← {snap}")

        with self._connect() as conn:
            conn.execute("DELETE FROM tool_calls WHERE step >= ?", (target_step,))
            conn.execute("DELETE FROM file_ops  WHERE step >= ?", (target_step,))

        for d in self.shadow_dir.glob("step-*"):
            try:
                n = int(d.name.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if n >= target_step:
                shutil.rmtree(d, ignore_errors=True)

    # ============================================================
    #  统一入口：两个世界一起回滚
    # ============================================================

    def rollback(self, target_step: int):
        """★ 同步回滚：文件 + 上下文，一个 step 都不差。

        方案 A 下 self.step 表示「当前/最近所处的步号」（0 起），
        合法目标是 0 <= target_step <= self.step（撤销 target 及其之后的所有步）。
        所以 target_step == self.step 是合法的（撤销最近这一步），
        只有 target_step > self.step（回滚到还没走过的未来）才报错。
        """
        if target_step > self.step:
            raise ValueError(
                f"target_step={target_step} > 当前 step={self.step}，只能回滚到已走过的步"
            )

        self._rollback_files(target_step)
        self._rollback_context(target_step)
        self.step = target_step
        print(f"[rollback] 已回滚到 step-{target_step:04d} 之前，当前 step = {self.step}")

    # ============================================================
    #  调试用
    # ============================================================

    def dump_state(self):
        """打印两个世界的当前状态（文件路径都用绝对路径）。"""
        with self._connect() as conn:
            print("  tool_calls:")
            for step, tool, inp, out in conn.execute(
                "SELECT step, tool, input, output FROM tool_calls ORDER BY step"
            ):
                print(f"    step={step:<3} {tool:<12} input={inp!r} output={out!r}")

            print("  file_ops:")
            for step, op, rel in conn.execute(
                "SELECT step, op, rel_path FROM file_ops ORDER BY step"
            ):
                print(f"    step={step:<3} {op:<8} {self._abs(rel)}")

        print("  上下文（messages）:")
        for m in self.messages:
            print(f"    step={m.step!r:<5} role={m.role:<9} {m.content!r}")
        print(f"  self.step = {self.step}")
