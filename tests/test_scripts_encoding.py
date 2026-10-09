"""脚本输出的编码下限：把 #39 从"修好一个脚本"升成"这一族都不许再犯"。

现场：Windows 上 stdout 被重定向或管道接走时，解释器按本地码页编码（本机实测 cp936）。
`run_eval.py` 修完之后，同一个坑在 `scripts/e2e_packs.py` 上原样复现——14 项验收全过，
最后打印 `✓` 时抛 `UnicodeEncodeError`、非零退出。**一次成功的事情被量具自己报成失败**，
这个形状与一次真回归完全同形，所以它属于"按族修"而不是"按点修"。

这里的下限有两条：① 共用一个 `harden_streams()`，不在每个脚本里各写一遍 reconfigure；
② 这条静态检查——脚本的**字符串字面量**里出现本地码页编不出的字符，就必须调用它。
注释与 docstring 里的 ⇒ 不算（不会被打印），所以检查只看非 docstring 的字符串常量。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((PROJECT_ROOT / "scripts").glob("*.py"))
LOCAL_CODEPAGE = "gbk"  # 本机实测：sys.stdout.encoding 在重定向时就是它


def _unprintable_literals(tree: ast.AST) -> list[str]:
    """返回脚本里"可能被打出去、但本地码页编不下"的字符串字面量。"""
    docstrings = {
        id(node.body[0])
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        try:
            node.value.encode(LOCAL_CODEPAGE)
        except UnicodeEncodeError:
            offenders.append(node.value[:40])
    return offenders


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_that_print_codepage_outside_chars_harden_their_streams(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"))
    offenders = _unprintable_literals(tree)
    if not offenders:
        return
    assert "harden_streams" in script.read_text(encoding="utf-8"), (
        f"{script.name} 会打印本地码页编不出的字符（如 {offenders[0]!r}），"
        "却没有 harden_streams()：一次全过的运行会在打印时被报成崩溃"
    )


def test_harden_streams_is_one_shared_implementation():
    """共用一个实现，不在每个脚本里各 reconfigure 一遍——两处各写就会出现一处漏修。"""
    sources = {path.name: path.read_text(encoding="utf-8") for path in SCRIPTS}
    re_implementations = [
        name for name, text in sources.items() if "stream.reconfigure(" in text
    ]
    assert not re_implementations, f"这些脚本自己 reconfigure 了：{re_implementations}"


def test_shared_helper_actually_survives_a_gbk_pipe():
    """这条是"能不能真拦住"的实测：强制 GBK 管道，打一个码页外字符，要求进程正常退出。"""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, r'" + str(PROJECT_ROOT / "src") + "');"
        "from agentflow.core.streams import harden_streams;"
        "harden_streams();print('✔ 全过')"
    )
    probe = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PYTHONIOENCODING": LOCAL_CODEPAGE, "SYSTEMROOT": str(Path.home())},
    )
    assert probe.returncode == 0, probe.stderr[-300:]
    assert "全过" in probe.stdout
def test_scripts_harden_their_streams_before_argparse_runs():
    """下限从"调了 harden_streams"升到"**在会打印的那些语句之前**调"。

    这条是被实测逼出来的：`load_test.py` 一直调着这个函数，但调在 `parse_args()` 之后，
    于是 `load_test.py --help` 在本机码页（cp936）下直接 `UnicodeEncodeError`——
    "我只是看看怎么用"被报成一次崩溃，而它的形状与一次真回归完全一样。
    判据（按源码行序，不按 AST 位置）：在 `main()` 里，**第一次出现 `harden_streams(` 的行号**
    必须早于第一次出现 `parse_args(`、`print(` 的行号。写成"必须是 main() 第一句"过严——
    有的脚本 main() 开头是 `global`，那不是打印路径。
    """
    checked: list[str] = []
    for script in SCRIPTS:
        source = script.read_text(encoding="utf-8")
        if "harden_streams(" not in source:
            continue
        lines = source.split(chr(10))
        starts = next(i for i, line in enumerate(lines) if line.startswith("def main("))
        body = lines[starts:]
        stop = next((i for i, line in enumerate(body[1:], 1) if line.startswith("def ") or line.startswith("class ")), len(body))
        body = body[:stop]
        def first_call(name: str) -> int:
            for i, line in enumerate(body):
                if line.lstrip().startswith("#"):
                    continue
                if name in line:
                    return i
            return len(body) + 1

        hardened = first_call("harden_streams(")
        assert hardened < first_call("parse_args("), f"{script.name}：harden_streams 排在 parse_args 之后"
        assert hardened < first_call("print("), f"{script.name}：harden_streams 排在第一次 print 之后"
        checked.append(script.name)
    assert {"load_test.py", "run_eval.py"} <= set(checked), f"这两个脚本没被这条下限看到：{checked}"


def test_load_test_help_survives_a_gbk_console():
    """活体：强制 GBK 码页跑 `load_test.py --help`，要求退出码 0 且打印出用法。

    只断"没崩"会放行"没崩但也没输出"，所以两条一起断（与 #39 那条回归用例同一形状）。
    环境用**继承 + 覆盖一个键**而不是整份替换：这个脚本 import asyncio，
    只给 SYSTEMROOT 的最小环境会让 `_overlapped` 在 Winsock 初始化上炸
    （WinError 10106），那种红在脚手架上、与被测无关。
    """
    import os
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "load_test.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": LOCAL_CODEPAGE},
    )
    assert probe.returncode == 0, (probe.stdout[-300:], probe.stderr[-500:])
    assert "usage" in probe.stdout.lower(), probe.stdout[-300:]
