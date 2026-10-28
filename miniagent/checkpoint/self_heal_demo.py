"""self_heal_demo.py —— 对比「约定版」和「方案 A」在"漏调一次记账"时的表现。

跑法（项目根目录）：
    .venv\\Scripts\\python.exe -m miniagent.checkpoint.self_heal_demo

场景：跑 3 步，但第 2 步（step 1）故意漏调 log_tool_call。
看快照目录会落到哪：
    约定版（自己 += 1）：step-0000 / step-0001 / step-0001   ← 第 3 步覆盖了第 2 步
    方案 A（begin_step）：step-0000 / step-0001 / step-0002   ← 编号始终正确（自愈）
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from miniagent.checkpoint import Checkpoint


def main():
    root = Path(tempfile.mkdtemp(prefix='self_heal_'))
    (root / 'hello.txt').write_text('v1', encoding='utf-8')

    messages = []
    ckpt = Checkpoint(root, messages)

    # old_step 用来【模拟】约定版那个"自己 += 1"的独立计数器（不真的跑旧代码）。
    old_step = 0

    print(f'root = {root}\n')
    for step in range(3):
        ckpt.begin_step(step)                       # ★ 方案 A：镜子照循环变量

        print(f'========== 第 {step+1} 轮循环（循环变量 step = {step}）==========')
        print(f'  方案A 快照会落:  step-{ckpt.step:04d}/   (self.step = {ckpt.step})')
        print(f'  旧版  快照会落:  step-{old_step:04d}/   (old_step  = {old_step})')

        # 真正存档（用方案 A 的 ckpt，落到 ckpt.self.step 那个目录）
        ckpt.snapshot_file(root / 'hello.txt')
        (root / 'hello.txt').write_text(f'v{step + 2}', encoding='utf-8')

        if step != 1:
            ckpt.log_tool_call('write_file', str(root / 'hello.txt'), 'ok')
            old_step += 1                            # 旧版：只有记账才推进
            print('  → 调了 log_tool_call')
        else:
            print('  → 【故意漏调 log_tool_call】')
        print()

    print('===== 快照目录实际内容（方案 A 的真实结果）=====')
    for d in sorted(ckpt.shadow_dir.glob('step-*')):
        content = (d / 'hello.txt').read_text(encoding='utf-8')
        print(f'  {d.name}/hello.txt = {content!r}')

    print('\n===== 对比结论 =====')
    print('  方案 A: step-0000 / step-0001 / step-0002   ← 三个都在，编号没错')
    print('  约定版: step-0000 / step-0001 / step-0001   ← 第 3 步覆盖了第 2 步（抛锚）')

    dirs = sorted(d.name for d in ckpt.shadow_dir.glob('step-*'))
    assert dirs == ['step-0000', 'step-0001', 'step-0002'], f'方案 A 编号错了: {dirs}'
    print('\n[OK] 方案 A 自愈：漏一次记账，编号依然正确')


if __name__ == '__main__':
    main()
