"""CLI 入口：多智能体协作的自动化数据分析引擎（Phase 4）。

用法示例：
  python scripts/run_analysis.py --data demo/data/retail_sales.csv --question "总销售额是多少？" --mode mock
  python scripts/run_analysis.py --data a.csv b.tsv alerts.json --question "生产域主机的异常登录有哪些？"
  python scripts/run_analysis.py --bundle outputs/bundles/bd_xxxx --question "总销售额是多少？"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证未安装（可编辑安装）时也能从源码导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentflow.core.config import load_dotenv  # noqa: E402
from agentflow.pipeline import run_analysis  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="基于多智能体协作的自动化数据分析引擎",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--data",
        nargs="+",
        help="输入文件（可多个，异构）：csv/tsv/json/jsonl 归一化成表，txt/log/md 作证据文档",
    )
    parser.add_argument(
        "--bundle",
        default=None,
        help="已建好的 Bundle 目录（含 manifest.json）；与 --data 二选一",
    )
    parser.add_argument("--question", required=True, help="中文业务问题，如：总销售额是多少？")
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="mock",
        help="mock=离线确定性模式（无需 API Key）；real=调用真实 LLM（需 .env 配置）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（留空则用项目默认 config/agents.yaml）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出根目录（默认 <项目根>/outputs）",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="会话 ID（多轮对话记忆，支持延续性提问）",
    )
    parser.add_argument(
        "--pack",
        default=None,
        help="场景包名称（如 login_audit 登录日志安全审计， packs/<name>/）",
    )
    return parser


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")
    args = build_parser().parse_args()

    if bool(args.data) == bool(args.bundle):
        print("--data 与 --bundle 必须二选一（给一组文件，或给一个已建好的 Bundle 目录）")
        return 2

    sources: object = args.data or args.bundle
    if args.bundle:
        from agentflow.core.bundle import Bundle

        sources = Bundle.load(args.bundle)

    result = run_analysis(
        question=args.question,
        sources=sources,
        config_path=args.config,
        mode=args.mode,
        outputs_root=args.output,
        session_id=args.session,
        pack=args.pack,
    )
    bundle = result.get("bundle")
    report = result.get("report") or {}
    print("=" * 48)
    print(f"run_id   : {result.get('run_id')}")
    if bundle is not None:
        print(f"输入     : {bundle.summary()}")
        if bundle.skipped:
            print(f"未入包   : {len(bundle.skipped)} 个文件（{bundle.skipped[0]['file']}：{bundle.skipped[0]['reason']}）")
    print(f"状态     : {result.get('status')}")
    print(f"输出目录 : {result.get('outputs_dir')}")
    if report.get("report_path"):
        print(f"报告路径 : {report.get('report_path')}")
    print("=" * 48)
    return 0


if __name__ == "__main__":
    sys.exit(main())
