"""批量跑测并记录到评估记录.md（评估方案 §6/§7 的执行入口）。

用法：
  python scripts/run_batch.py                       # 跑默认 mock 批次（测试用例清单 M1~M5）
  python scripts/run_batch.py --name "我的批次"      # 自定义批次名
  python scripts/run_batch.py --outputs outputs/batch_x   # 指定产物目录

流程：逐题 run_analysis → 写入独立批次目录 → 调 evaluate.py 聚合 → 追加《评估记录.md》。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.core.config import load_dotenv  # noqa: E402
from agentflow.pipeline import run_analysis  # noqa: E402

DATA = PROJECT_ROOT / "demo" / "data" / "retail_sales.csv"
DATA_PROFIT = PROJECT_ROOT / "demo" / "data" / "retail_sales_with_profit.csv"
LOGIN_ATTACK = PROJECT_ROOT / "demo" / "data" / "login_auth.csv"
LOGIN_NORMAL = PROJECT_ROOT / "demo" / "data" / "login_auth_normal.csv"

# 默认批次 = 测试用例清单 M1~M5（mock 模式，离线确定性，可纵向对比）
MOCK_CASES: list[dict[str, Any]] = [
    {"id": "M1", "question": "总销售额是多少？", "data": DATA_PROFIT},
    {"id": "M2", "question": "一共有多少笔订单？", "data": DATA_PROFIT},
    {"id": "M3", "question": "最近7天每日销售额的走势如何？", "data": DATA_PROFIT},
    {"id": "M4", "question": "哪个产品类别卖得最好？", "data": DATA_PROFIT},
    {"id": "M5", "question": "分析一下上周的利润情况", "data": DATA},
]

# 场景包批次（--suite pack）= 登录日志安全审计（攻击数据 4 规则命中 + 正常数据零发现）
PACK_CASES: list[dict[str, Any]] = [
    {"id": "P1", "question": "对2026-09-05的登录日志做安全审计", "data": LOGIN_ATTACK, "pack": "login_audit"},
    {"id": "P2", "question": "对登录日志做安全审计", "data": LOGIN_NORMAL, "pack": "login_audit"},
]


def main() -> int:
    parser = argparse.ArgumentParser(description="批量跑测并记录评估")
    parser.add_argument("--name", default=None, help="批次名（默认含日期时间）")
    parser.add_argument("--outputs", default=None, help="批次产物目录（默认 outputs/batch_<ts>）")
    parser.add_argument("--mode", default="mock", choices=["mock", "real"])
    parser.add_argument(
        "--suite",
        default="mock",
        choices=["mock", "pack"],
        help="批次用例集：mock=M1~M5 零售；pack=场景包（登录日志安全审计）",
    )
    parser.add_argument(
        "--env-file", default=None, help="real 模式的 .env 路径（默认取脚本所在项目根的 .env）"
    )
    parser.add_argument("--no-record", action="store_true", help="只跑不写评估记录")
    args = parser.parse_args()

    if args.mode == "real":
        env_path = Path(args.env_file) if args.env_file else PROJECT_ROOT / ".env"
        if not env_path.exists():
            print(f"real 模式需要 .env（含 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL）：{env_path} 不存在")
            return 2
        load_dotenv(env_path)
        import os

        if not os.getenv("OPENAI_API_KEY"):
            print(f"{env_path} 中缺少 OPENAI_API_KEY")
            return 2
        print(f"LLM: {os.getenv('OPENAI_BASE_URL')} ｜ model: {os.getenv('LLM_MODEL')}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch_name = args.name or f"{time.strftime('%Y-%m-%d %H:%M')} {args.mode} 批次"
    outputs_root = Path(args.outputs) if args.outputs else PROJECT_ROOT / "outputs" / f"batch_{stamp}"
    outputs_root.mkdir(parents=True, exist_ok=True)

    print(f"批次：{batch_name}\n产物：{outputs_root}\n")
    cases = PACK_CASES if args.suite == "pack" else MOCK_CASES
    failed = 0
    for case in cases:
        started = time.monotonic()
        try:
            result = run_analysis(
                question=case["question"],
                sources=case["data"],
                mode=args.mode,
                outputs_root=outputs_root,
                session_id=case.get("session"),
                pack=case.get("pack"),
            )
            status = result["status"]
        except Exception as exc:  # noqa: BLE001 - 单题异常不阻断批次
            status = f"exception: {str(exc)[:120]}"
            failed += 1
        elapsed = round(time.monotonic() - started, 2)
        if status not in ("success", "partial", "degraded"):
            failed += 1
        print(f"  {case['id']:3} {status:10} {elapsed:6.2f}s  {case['question']}")

    if args.no_record:
        return 1 if failed else 0
    evaluate = PROJECT_ROOT / "scripts" / "evaluate.py"
    code = subprocess.call(
        [
            sys.executable,
            str(evaluate),
            "--outputs",
            str(outputs_root),
            "--batch",
            batch_name,
        ],
        cwd=str(PROJECT_ROOT),
    )
    return 1 if (failed or code) else 0


if __name__ == "__main__":
    sys.exit(main())
