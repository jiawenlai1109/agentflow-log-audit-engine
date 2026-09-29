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
