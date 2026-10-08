"""把"临时名写好后整体换名"这一步收成一个函数：Windows 上目标被读者占用时要撑住。

为什么要有这个文件（不是提前优化，是被一次偶发红逼出来的）：
2026-10-08 全量回归第三次跑到 `PermissionError: [WinError 5]`，形状是
`os.replace(staging, target)` 在把归一化表落盘的那一步被拒。写了个探针量化它
（`.appdata/probe_atomic_rename.py`，8 个线程写同一个 target + 2 个线程读，240 次替换）：
这台 Windows 上三轮分别被拒 **81 / 91 / 90** 次。所以这不是运气，而是这条路径在这类
系统上的固有形状——**换名要撞的正是"目标还被另一个读者开着"的那几毫秒**
（Windows 的改名要求目标可删除，而 Python 开文件不带 `FILE_SHARE_DELETE`）。

三道闸，按"代价从低到高"排：

1. **目标已经是这一份内容 ⇒ 不换名，直接认。** 归一化缓存的目录名是按源文件路径+mtime 算的
   指纹，同一家企业里两个人冷启动同一批数据，写出来的字节是一样的（`_store_table` 的列与
   编码都由源决定）。既然字节相同，"谁发布"没有意义，而**少一次换名就少一次撞句柄的机会**。
   这一条把绝大多数并发直接消掉了，比事后重试更对。
2. **只重试 `PermissionError`**，递增退避。磁盘满、路径非法、跨设备这类 `OSError`
   重试不会变好，立刻原样抛出去，让人当场看见。
3. **预算用完必须把最后一次异常抛出去**。静默退化成"直接写目标"是不行的：那等于把这次发布
   变成可被读到的半成品，而整套缓存判据（"manifest 在场 = 全套都在"）就靠这一步是原子的。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 6 次、递增退避：最坏 0.05×(1+2+3+4+5) ≈ 0.75s。读者的句柄通常是毫秒级，
# 而一次归一化本来就要几十毫秒——这个预算换掉的是"整次运行失败"。
REPLACE_ATTEMPTS = 6
REPLACE_WAIT_S = 0.05
_CHUNK = 64 * 1024


def _already_there(staging: Path, target: Path) -> bool:
    """目标是否已经就是这份内容（先比大小，再逐块比；两边都不占整份内存）。"""
    try:
        staged, live = staging.stat().st_size, target.stat().st_size
    except OSError:
        return False
    if staged != live:
        return False
    try:
        with staging.open("rb") as left, target.open("rb") as right:
            while True:
                a, b = left.read(_CHUNK), right.read(_CHUNK)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        # 比不了就当不相同，走换名那条路（宁可多做一次，不许把不同内容认成相同）
        return False


def replace_atomically(
    staging: Path,
    target: Path,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    wait_s: float = REPLACE_WAIT_S,
) -> bool:
    """把 `staging` 换成 `target`。

    返回 True = 换名真的发生了；返回 False 但没抛异常 = 目标已经是这份内容，不必换名。
    调用方不用区分（`_write_atomically` 只问"最终在场的是不是完整一份"），但测试要能问——
    所以这个结论做成返回值，不藏在日志里。
    """
    if target.exists() and _already_there(staging, target):
        return False
    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(staging, target)
            return True
        except PermissionError as error:
            last_error = error
            if _already_there(staging, target):
                # 撞开的这几毫秒里，另一个进程/线程已经把同样一份发布好了
                return False
            if attempt + 1 < attempts:
                time.sleep(wait_s * (attempt + 1))
        except OSError:
            # 别的 OS 错误（磁盘满 / 路径非法 / 跨设备）不是"再试一次"能解决的
            raise
    assert last_error is not None, "循环没走通却没有异常，这条不可能到达"
    raise last_error
