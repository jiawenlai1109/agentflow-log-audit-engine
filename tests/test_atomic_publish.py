"""归一化落盘的"整体换名"在并发读者面前必须站得住（缺陷 #53）。

起因是 2026-10-08 全量回归第三次跑到一次 `PermissionError: [WinError 5]`：
`os.replace(staging, target)` 在把归一化表放进 Bundle 缓存时被拒。写了个探针量化它
（`.appdata/probe_atomic_rename.py`，8 个线程写同一个目标 + 2 个线程读，240 次替换）：
这台 Windows 上三轮分别被拒 **81 / 91 / 90** 次。所以这条不是运气，是这条路径在这类
系统上的固有形状——**换名要撞的正是"目标还被另一个读者开着"的那几毫秒**
（Windows 改名要求目标可删除，而 Python 开文件不带 `FILE_SHARE_DELETE`）。

这七条用例盯的是修完之后不许出现的滑坡：
① 撞一下就整条 run 失败（偶发变常态）；② 撞一下就静默当成发布成功（半成品上桌）；
③ 重试把所有 `OSError` 都吞掉（磁盘满也"再试一次"）；④ "已经在了"只比大小不比字节
（把不同内容认成相同，比失败更糟）；⑤ 只修一处，另一处照旧裸换名；
⑥ 该短路的没短路（每次冷启动都真去换名，等于把并发机会重新请回来）。

**读侧那一半没有修**：探针显示写侧修好之后，读侧仍会被短暂拒绝（6 写 2 读、约 140 次读里
1–2 次）。引擎的读路径（pandas / `read_text`）没有重试，所以并发用例把读侧被拒的次数
**打印出来但不算成已修**——那是记给 P1/P5 的一条账，不是这条测试的功劳。
"""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path

import pytest

from agentflow.core.atomic import REPLACE_ATTEMPTS, replace_atomically
from agentflow.core.ingest import _write_atomically

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _staging_and_target(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "shared.csv"
    target.write_text("a,b\n0,0\n", encoding="utf-8")
    staging = tmp_path / ".shared.csv.tmp-new"
    staging.write_text("a,b\n9,9\n", encoding="utf-8")
    return staging, target


def test_transient_share_denial_still_publishes(tmp_path, monkeypatch):
    """被拒两次就成功：内容必须是新的那一份，临时名不能留在盘上。"""
    staging, target = _staging_and_target(tmp_path)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "拒绝访问（模拟读者的句柄）")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    replace_atomically(staging, target)
    assert target.read_text(encoding="utf-8") == "a,b\n9,9\n"
    assert not staging.exists()
    assert calls["n"] == 3, "重试次数应当刚好跨过两次瞬时拒绝"


def test_persistent_share_denial_raises_instead_of_lieing(tmp_path, monkeypatch):
    """一直撞不开必须抛出去——静默返回等于把"没发布成功"说成"发布好了"。"""
    staging, target = _staging_and_target(tmp_path)
    attempts: list[int] = []

    def always_denied(src, dst, *args, **kwargs):
        attempts.append(1)
        raise PermissionError(13, "拒绝访问（模拟一直占着目标的读者）")

    monkeypatch.setattr(os, "replace", always_denied)
    with pytest.raises(PermissionError):
        replace_atomically(staging, target)
    assert len(attempts) == REPLACE_ATTEMPTS, len(attempts)
    # 旧内容原样留着：半成品不许被端上桌
    assert target.read_text(encoding="utf-8") == "a,b\n0,0\n"


def test_other_os_error_is_not_retried(tmp_path, monkeypatch):
    """磁盘满 / 路径非法这类错误重试不会变好，必须当场报（只重试"撞不开"那一种）。"""
    staging, target = _staging_and_target(tmp_path)
    calls = {"n": 0}

    def broken(src, dst, *args, **kwargs):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        replace_atomically(staging, target)
    assert calls["n"] == 1, f"非 PermissionError 不该重试：跑了 {calls['n']} 次"


def test_concurrent_publish_never_exposes_a_half_file(tmp_path):
    """缺陷 #53 的复现路径：同一家企业里多人冷启动同一批数据 ⇒ 多个写者换同一个目标。

    写的内容刻意完全相同——这就是 app 里的真实形状：`bd_<指纹>` 目录名由源文件路径+mtime 决定，
    同一批数据归一化出来的字节也一样。

    两侧分别断言，因为它们是两笔不同的账：

    - **写侧必须零逃逸**（这就是这次修的东西：同样内容不再换名 + 撞不开时重试）。
    - **读侧永远只能拿到完整一份**（不许读到半成品）。而读侧在那几毫秒里被拒一下，
      是 Windows 上"有人在换名"的另一半问题——引擎的读路径（pandas / read_text）没有重试，
      这条**没有**被本次改动消掉，所以这里只统计、不算成已修；实测数字写进工作日志的 #53。
    """
    target = tmp_path / "shared.csv"
    body = "a,b\n" + "\n".join(f"{index},{index * 2}" for index in range(50)) + "\n"
    writers, readers, rounds = 6, 2, 10
    write_errors: list[str] = []
    half_files: list[str] = []
    read_denied = {"n": 0}
    reads_ok = {"n": 0}
    stop = threading.Event()
    lock = threading.Lock()

    def write_one() -> None:
        for _round in range(rounds):
            try:
                _write_atomically(target, lambda staging: staging.write_text(body, encoding="utf-8"))
            except OSError as error:
                with lock:
                    write_errors.append(f"{type(error).__name__}: {str(error)[:120]}")

    def read_one() -> None:
        while not stop.is_set():
            try:
                if target.exists():
                    lines = target.read_text(encoding="utf-8").strip().splitlines()
                    with lock:
                        if len(lines) != 51:
                            half_files.append(f"读到 {len(lines)} 行")
                        reads_ok["n"] += 1
            except PermissionError:
                with lock:
                    read_denied["n"] += 1

    threads = [threading.Thread(target=write_one) for _ in range(writers)]
    readers_ = [threading.Thread(target=read_one) for _ in range(readers)]
    for thread in threads + readers_:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    for thread in readers_:
        thread.join(timeout=5)

    assert not write_errors, f"写侧仍有 {len(write_errors)} 次逃逸：{write_errors[:3]}"
    assert not half_files, f"读侧看到过半成品：{half_files[:3]}"
    assert not list(tmp_path.glob(".*.tmp-*")), "临时名必须被清掉"
    assert target.read_text(encoding="utf-8") == body
    assert reads_ok["n"] > 0, "读侧一次都没读到，这条用例就没有观察点"
    # 读侧被拒的次数不许伪装成 0：把它打印出来，#53 的剩余部分才有数可讲
    print(f"读侧成功 {reads_ok['n']} 次，读侧短暂被拒 {read_denied['n']} 次（引擎读路径未重试）")


def test_different_content_is_never_accepted_as_already_there(tmp_path, monkeypatch):
    """"目标已经有这份内容"必须真的比过字节——把不同内容认成相同，比失败更糟。"""
    from agentflow.core import atomic

    staging = tmp_path / ".shared.csv.tmp-new"
    target = tmp_path / "shared.csv"
    target.write_text("a,b\n1,1\n", encoding="utf-8")
    staging.write_text("a,b\n2,2\n", encoding="utf-8")
    assert atomic._already_there(staging, target) is False
    staging.write_text("a,b\n1,1\n", encoding="utf-8")
    assert atomic._already_there(staging, target) is True
    # 大小相同、内容不同：逐块比较必须抓到
    staging.write_text("a,b\n9,9\n", encoding="utf-8")
    assert atomic._already_there(staging, target) is False
    # 目标不在 ⇒ 不能说"已经在"
    missing = tmp_path / "nope.csv"
    assert atomic._already_there(staging, missing) is False


def test_skip_when_target_already_has_it(tmp_path, monkeypatch):
    """同样内容不再换名：换名次数为 0，而调用方拿到的仍是完整一份（这才是 #53 的主修）。"""
    calls = {"n": 0}
    real = os.replace

    def counting(src, dst, *args, **kwargs):
        calls["n"] += 1
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", counting)
    target = tmp_path / "shared.csv"
    body = "a,b\n1,1\n"
    for _ in range(3):
        _write_atomically(target, lambda staging: staging.write_text(body, encoding="utf-8"))
    assert calls["n"] == 1, f"第一次之后内容就没变过，不该再换名：{calls['n']} 次"
    assert target.read_text(encoding="utf-8") == body
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_the_rename_has_exactly_one_home():
    """裸 `os.replace` 只许住在 `atomic.py`。

    修这类偶发时最怕的就是"一处修了、另一处照旧"：#23 那次给归一化写加了原子替换，
    但"原子替换撞上读者的句柄"这一层当时没人管，于是同一个形状在 `ingest` 与 `Bundle.write`
    两处各自裸露。守卫按 AST 查，不看注释也不看字符串。
    """
    offenders: list[str] = []
    for py in sorted((PROJECT_ROOT / "src" / "agentflow").rglob("*.py")):
        if py.name == "atomic.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                offenders.append(f"{py.name}:{node.lineno}")
    assert offenders == [], "换名出现了第二处实现：" + ", ".join(offenders)
