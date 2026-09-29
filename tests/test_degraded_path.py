"""降级路径的两条缺陷（④ traceback 进正文 / ⑤ 缺列分类不可行动）。

两条都是"报告说给人听的那句话出了问题"：
④ 面向用户的 `report.md` 直接落原始 traceback ⇒ 泄露 `.venv/...` 与项目绝对路径，
   而 pandas 的内部行号还会被追溯率当成"报告里冒出没有出处的数字"；
⑤ 场景包缺必需列时落成 `run_error` / `错误分类：UNKNOWN` / `建议：检查运行日志` ⇒
   明明可行动的信息（缺哪几列、约定写在哪个文件）被压成一句让人去翻日志的废话。

共同的立场是：**展示层脱敏，事实层不删**——完整 traceback 必须仍然能在 transcript 里查到。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow.core.orchestrator import display_error
from agentflow.core.pack import PackContractError, load_pack
from agentflow.pipeline import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRIAGE = PROJECT_ROOT / "demo" / "data" / "triage"

# 一条真实形态的子进程 stderr（E07 现场：缺"利润率"列）
REAL_TRACEBACK = (
    'Traceback (most recent call last):\n'
    f'  File "{PROJECT_ROOT}\\outputs\\work\\2\\script.py", line 6, in <module>\n'
    "    print(df['利润率'].sum())\n"
    '          ~~^^^^^^^^^^\n'
    f'  File "{PROJECT_ROOT}\\.venv\\Lib\\site-packages\\pandas\\core\\frame.py", line 4378, in __getitem__\n'
    "    indexer = self.columns.get_loc(key)\n"
    f'  File "{PROJECT_ROOT}\\.venv\\Lib\\site-packages\\pandas\\core\\indexes\\base.py", line 3648, in get_loc\n'
    "    raise KeyError(key) from err\n"
    "KeyError: '利润率'"
)


def test_display_error_keeps_the_actionable_line_and_drops_the_environment():
    shown = display_error(REAL_TRACEBACK)
    assert "利润率" in shown, shown
    # 绝对路径与环境布局不许出现在给人看的正文里
    assert ".venv" not in shown and "site-packages" not in shown, shown
    assert str(PROJECT_ROOT)[:8] not in shown, shown
    # 库内部行号既不是信息也不是出处，两头都是害
    assert "4378" not in shown and "3648" not in shown, shown


def test_display_error_handles_the_shapes_it_meets():
    assert display_error("") == "（无错误详情）"
    assert display_error(None) == "（无错误详情）"
    # 没有异常摘要行时退回首行，而不是编造一句
    assert display_error("数据缺少场景包 sigma_triage 必需列：是否生产").startswith("数据缺少")
    # 多行里没有 Error 摘要时取首行；首行本身就是摘要
    assert display_error("RuntimeError: 预算耗尽\n其他噪声") == "RuntimeError: 预算耗尽"
    long = display_error("ValueError: " + "x" * 900)
    assert len(long) < 400 and "完整记录见" in long


def test_pack_contract_error_carries_what_the_user_can_act_on():
    pack = load_pack("sigma_triage")
    error = PackContractError(pack.name, ["是否生产"], pack.required_columns)
    assert error.missing == ["是否生产"]
    assert error.error_class == "MISSING_COLUMN"
    # 文案三件事缺一不可：缺哪列 / 去哪看约定 / 为什么不做半套审计
    assert "是否生产" in error.suggestion and "data_convention" in error.suggestion
    assert "必需列" in str(error)


def _run_missing_assets(tmp_path: Path) -> dict[str, Any]:
    """只给两张表（资产表缺席）⇒ "生产域"这个概念根本不在这批数据里。"""
    sources = [TRIAGE / "auth.csv", TRIAGE / "edr.csv"]
    return run_analysis(
        question="生产域主机的异常告警有哪些？哪些需要立刻处置",
        sources=[str(path) for path in sources],
        mode="mock",
        outputs_root=tmp_path,
        pack="sigma_triage",
    )


def test_missing_pack_column_degrades_with_an_actionable_classification(tmp_path):
    result = _run_missing_assets(tmp_path)
    assert result["status"] == "degraded"
    evaluation = json.loads((Path(result["outputs_dir"]) / "evaluation.json").read_text(encoding="utf-8"))
    # 不再是 run_error：分类要能指到"数据契约不满足"这一格
    assert evaluation["degraded_reason"] == "pack_contract"

    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    assert "错误分类：MISSING_COLUMN" in report, report[:600]
    assert "检查运行日志" not in report, "又退回了那句让人去翻日志的废话"
    assert "data_convention" in report, report[:600]


class _BoomLLM:
    """一开始就抛"带 traceback 的错误"：走的是运行级兜底那条出口（`_write_degraded_report`）。"""

    budget = None

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError(self.text)

    def complete_structured(self, *args: Any, **kwargs: Any):
        raise RuntimeError(self.text)


def test_run_level_degraded_report_is_sanitized_too(tmp_path):
    """降级报告有两条出口：任务失败的模板与运行级兜底。只堵一条等于没堵。"""
    result = run_analysis(
        question="各品类销售额是多少？",
        sources=str(PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"),
        mode="mock",
        llm=_BoomLLM(REAL_TRACEBACK),
        outputs_root=tmp_path,
    )
    assert result["status"] == "degraded"
    report = Path(result["report"]["report_path"]).read_text(encoding="utf-8")
    assert ".venv" not in report and "site-packages" not in report, report[:600]
    assert str(PROJECT_ROOT)[:8] not in report, report[:600]
    assert "利润率" in report, "脱敏之后仍要说清到底哪儿错了"


def test_sanitizing_the_report_does_not_blind_the_fact_layer(tmp_path):
    """脱敏只发生在给人看的那一段：异常的来路必须仍然查得到，否则"脱敏"就变成"丢证据"。"""
    result = _run_missing_assets(tmp_path)
    outputs = Path(result["outputs_dir"])
    records = [
        json.loads(line)
        for line in (outputs / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    frames = [r for r in records if r.get("event") == "run_failed_traceback"]
    assert frames, "降级不留痕 = 又一次静默降级"
    # 报告正文里被抹掉的类型名与栈帧，事实层一个字都不能少
    assert "PackContractError" in frames[-1]["traceback"]
