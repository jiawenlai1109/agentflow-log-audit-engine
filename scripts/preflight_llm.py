"""型号可用性预检（G2）：一次实测，产出一张"这个型号能不能在本项目里用"的表。

    python scripts/preflight_llm.py                     # 探 GET /models 列出的全部型号，两档都探
    python scripts/preflight_llm.py --models glm-5.3    # 只点名的那几个
    python scripts/preflight_llm.py --levels default    # 只探默认档（一半的真实调用）
    python scripts/preflight_llm.py --json              # 机器可读输出

为什么要有这支脚本：2026-10-06 实测同一台网关上 10 个型号——7 个开箱能用、2 个必须
`thinking: disabled`（其中一个默认档 87 秒后交白卷）、1 个**关了思考反而坏**。
这些只能实测，而且必须用引擎自己的客户端测：判据是"引擎能不能取出可用正文"，
不是"HTTP 是否 200"。

探针会产生**真实调用**（型号数 × 档数）。prompt 极短、`max_tokens` 极小，但开关在你手上：
默认一次跑满 20 次调用，`--models` / `--levels` 可以收窄。结果写到 `.appdata/llm_preflight.json`，
**里面没有正文，也没有凭据**（只有形状与耗时）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.core.config import load_config, load_dotenv  # noqa: E402
from agentflow.core.llm import LLMError, OpenAILLM  # noqa: E402
from agentflow.core.streams import harden_streams  # noqa: E402
from agentflow.core.llm_preflight import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    PROBE_VERSION,
    build_report,
    classify,
    concurrency_note,
    host_of,
    list_models,
    merge_concurrency,
    probe,
    probe_concurrency,
    read_cache,
    write_cache,
)

LEVELS = ("default", "disabled")
COLUMNS = 104


def _run_concurrency(args, config: dict, base_url: str, api_key: str) -> int:
    """`--concurrency` 模式：只量上游并发形状，不重探型号可用性。

    成本口径先打在屏幕上再动手——**这笔钱是花在读数上的，得让人看得见花在几次调用**。
    每档一波、退避关掉：带重试会把 429 藏进"最终成功"，量出来的配额会比真值大。
    """
    import os

    widths = [int(item) for item in args.widths.split(",") if item.strip().isdigit()]
    widths = [item for item in widths if item > 0]
    if not widths:
        print("--widths 一个都没解析出来（要写成 1,4,8 这样）。一次调用都不发。")
        return 1
    llm_cfg = config.get("llm", {}) or {}
    model = args.model or os.getenv("LLM_MODEL") or llm_cfg.get("model") or "gpt-4o-mini"
    thinking = llm_cfg.get("thinking") or None
    planned = sum(widths)
    print(f"端点 {base_url} ｜ 型号 {model} ｜ thinking={thinking or '（不发这个字段）'}")
    print(
        f"将产生 {planned} 次真实调用：并发档 {'/'.join(str(item) for item in widths)}，每档一波，"
        f"max_tokens={args.max_tokens}，传输层退避已关（不关就把 429 藏进成功里）"
    )

    def make_client() -> OpenAILLM:
        return OpenAILLM(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=120,
            max_retries=0,
            thinking=thinking,
        )

    concurrency = probe_concurrency(make_client, widths, max_tokens=args.max_tokens)
    concurrency["model"] = model
    concurrency["thinking"] = thinking
    path = merge_concurrency(Path(args.cache), base_url, concurrency)
    if args.json:
        print(json.dumps(concurrency, ensure_ascii=False, indent=2))
    else:
        print("-" * COLUMNS)
        for wave in concurrency["waves"]:
            latency = wave["latency"]
            print(
                f"  {wave['width']:>3} 路  ok={wave['ok']}/{wave['calls']}  拒={wave['refused']}  "
                f"墙钟={wave['wall_seconds']:>6}s  p50={latency['p50']}s p95={latency['p95']}s max={latency['max']}s"
                + ("  ← 排队" if wave["queued"] else "")
                + (f"  错误形状={wave['errors']}" if wave["errors"] else "")
            )
        print("-" * COLUMNS)
        print(concurrency_note(concurrency))
        print(f"已并入缓存：{path}（只有形状与计数，没有正文、没有凭据）")
    return 0


def main() -> int:
    harden_streams()
    parser = argparse.ArgumentParser(description="LLM 型号可用性与上游并发预检（产生真实调用）")
    parser.add_argument("--models", default="", help="逗号分隔的型号；留空 = 用 GET /models 列出的全部")
    parser.add_argument("--levels", default=",".join(LEVELS), help="逗号分隔：default / disabled")
    parser.add_argument("--base-url", default="", help="覆盖 OPENAI_BASE_URL（测别的站时用）")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--cache", default=str(PROJECT_ROOT / ".appdata" / "llm_preflight.json"))
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    parser.add_argument(
        "--concurrency",
        action="store_true",
        help="只测上游并发形状（P4 闸门的默认值出处）；配合 --model 点一个型号",
    )
    parser.add_argument("--widths", default="1,4,8", help="逗号分开的并发档，如 1,4,8")
    parser.add_argument("--model", default="", help="并发模式点名的型号（留空 = 用 .env/config 里那个）")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    import os

    config = load_config()
    llm_cfg = config.get("llm", {}) or {}
    base_url = args.base_url or os.getenv("OPENAI_BASE_URL") or llm_cfg.get("base_url") or ""
    api_key = os.getenv("OPENAI_API_KEY") or ""
    if not api_key:
        print("缺少 OPENAI_API_KEY（.env 或环境变量），预检中止——一次调用都不发。")
        return 1

    if args.concurrency:
        return _run_concurrency(args, config, base_url, api_key)

    levels = [level for level in args.levels.split(",") if level.strip()]
    if args.models.strip():
        models = [item.strip() for item in args.models.split(",") if item.strip()]
    else:
        try:
            models = list_models(base_url, api_key)
        except LLMError as exc:
            print(f"取型号清单失败：{exc}")
            print("→ 这不是「这条站不能用」，是清单接口本身没答出来；检查 OPENAI_BASE_URL 该不该带 /v1。")
            return 1
    if not models:
        print("型号清单是空的，没有可探的对象。")
        return 1

    planned = len(models) * len(levels)
    if not args.json:
        print(f"端点 {base_url}")
        print(f"将产生 {planned} 次真实调用（{len(models)} 个型号 × {len(levels)} 档，max_tokens={args.max_tokens}）")
        print(
            f"{'型号':<26} {'档':<9} {'可用':<5} {'整段JSON':<9} {'耗时':>7} {'正文字数':>7}  形状/错误"
        )
        print("-" * COLUMNS)

    entries: list[dict[str, object]] = []
    for model in models:
        levels_result: dict[str, dict[str, object]] = {}
        for level in levels:
            client = OpenAILLM(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout=120,
                max_retries=0,
                thinking=None if level == "default" else level,
            )
            record = probe(client, max_tokens=args.max_tokens)
            levels_result[level] = record
            if not args.json:
                shape = record.get("error") or f"finish={record.get('finish_reason') or 'stop'}"
                print(
                    f"{model:<26} {level:<9} {'是' if record['ok'] else '否':<5} "
                    f"{'是' if record.get('strict_json') else '否':<9} "
                    f"{record['seconds']:6.1f}s {record.get('chars', 0):>7}  {str(shape)[:40]}"
                )
        verdict, note = classify(levels_result)
        entries.append({"model": model, "verdict": verdict, "note": note, "levels": levels_result})
        if not args.json:
            print(f"{'':<26} ⇒ {verdict}：{note}")

    report = build_report(base_url, entries)
    cache_path = write_cache(report, Path(args.cache))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("-" * COLUMNS)
        print(f"结论缓存：{cache_path}（无正文、无凭据）")
        blocked = [entry["model"] for entry in entries if entry["verdict"] == "unusable"]
        if blocked:
            print(f"判死 {len(blocked)} 个：{'、'.join(blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
