"""脚本输出的编码下限：一次已经跑完的事情，不该被 stdout 的码页判成失败。

Windows 上 stdout 被重定向或管道接走时，解释器按本地码页编码（本机实测 cp936）。
脚本里的 ✔ ✘ ⇒ 这类码页外字符因此会在**最后一步打印结果时**抛 `UnicodeEncodeError`：
27 题全绿之后量具自己崩、活体验收 14 项全过之后探针非零退出——表现与一次真回归同形。

这里放一个共用入口，而不是每个脚本各写一遍：同一条修复只补一处出口，
下一次一定换个脚本再犯（`run_eval.py` 修完之后 `e2e_packs.py` 就是这么暴出来的）。
"""

from __future__ import annotations

import sys


def harden_streams(*, encoding: str = "utf-8", errors: str = "replace") -> None:
    """把 stdout/stderr 钉在指定编码上；换码页、换字符都不该再改变退出码。

    `errors="replace"` 是下限：将来再出现码页外字符，最坏是某个符号显示成 `?`，
    而不是把一次成功的运行报成崩溃。交互终端本来就走 UTF-8 通道，不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding=encoding, errors=errors)
