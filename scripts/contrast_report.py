"""对比度量具：把界面上用到的前景/背景色按 WCAG 2.1 相对亮度算一遍。

为什么单独一个脚本：这一轮的结论要进文档和用例断言，不能靠口算，也不能引用别人（子 agent）
给的数——本项目对数字的要求是"能在产物里找到出处"。用法：
    .venv/Scripts/python scripts/contrast_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.core.streams import harden_streams  # noqa: E402

# 硬编码在这里是有意的：这几对就是界面里真实出现过的组合，不是抽象的色板遍历。
CASES = [
    # (前景, 背景, 用在哪, 判定用途)
    ("#909399", "#ffffff", "当前：.hint/.meta/.drop .hint 二级说明文字（12px）", "block"),
    ("#909399", "#f5f7fa", "当前：同上，落在 el-card/fill-light 背景上", "block"),
    ("#606266", "#ffffff", "改后：--el-text-color-regular", "block"),
    ("#606266", "#f5f7fa", "改后：同上，浅底", "block"),
    ("#e6a23c", "#ffffff", "当前：--el-color-warning 直接当正文色（.risk / el-tag--warning 文字）", "block"),
    ("#fdf6ec", "#e6a23c", "当前：el-tag--warning 浅底上的深字（Tag 自带的那套）", "pass"),
    ("#a15c00", "#ffffff", "改后：自定的深一档 warning 正文色", "block"),
    ("#b88230", "#ffffff", "改后候选：EP 自带 --el-color-warning-dark-2", "block"),
    ("#b88230", "#fdf6ec", "改后：warning 浅底 Tag 里换用它", "block"),
    ("#73767a", "#f4f4f5", "改后候选：info 浅底 Tag 用 --el-color-info-dark-2", "pass"),
    ("#5c5f66", "#f4f4f5", "改后候选：info 浅底 Tag 再深一档", "pass"),
    ("#c45656", "#fef0f0", "改后候选：danger 浅底 Tag 用 --el-color-danger-dark-2", "pass"),
    ("#a03b3b", "#fef0f0", "改后候选：danger 浅底 Tag 再深一档", "pass"),
    ("#529b2e", "#f0f9eb", "改后候选：success 浅底 Tag 用 --el-color-success-dark-2", "pass"),
    ("#3f7a22", "#f0f9eb", "改后候选：success 浅底 Tag 再深一档", "pass"),
    ("#303133", "#ffffff", "主文本", "pass"),
    ("#909399", "#303133", "深底上的说明文字（若将来开暗色）", "pass"),
]

AA_NORMAL = 4.5  # 9pt~12pt 常规字
AA_LARGE = 3.0  # ≥18.66px 粗体 或 ≥24px
AA_NON_TEXT = 3.0  # 边框、图标这类可感知边界


def channel(v: int) -> float:
    c = v / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_: str) -> float:
    hex_ = hex_.lstrip("#")
    r, g, b = (int(hex_[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(fg: str, bg: str) -> float:
    hi, lo = max(luminance(fg), luminance(bg)), min(luminance(fg), luminance(bg))
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    harden_streams()
    rows = []
    fails = 0
    for fg, bg, where, kind in CASES:
        r = ratio(fg, bg)
        ok = r >= AA_NORMAL
        if kind == "block" and not ok:
            fails += 1
        rows.append(f"{r:5.2f}:1  {'AA✓' if ok else 'AA✗'}  [{kind}]  {fg} on {bg}  —— {where}")
    print("\n".join(rows))
    print(f"\n需要达标的组合里不合格：{fails} 对（判据 AA 常规字 {AA_NORMAL}:1）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
