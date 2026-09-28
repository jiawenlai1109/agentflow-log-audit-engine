"""生成 MCP 演示库 `demo/data/soc_intel.sqlite`（骨架的"外部数据源"）。

为什么需要它：M4-C 要证的是"外部结果只作证据、不进数字来源"。这就要求外部数据里有一个
**足够独特的数字**——一旦它漏进报告，追溯率那条闸门必须红。这里选 `4242`：
它不属于本次上传的任何一张表，任何一份报告里出现它都只能是外部串台。

行内容是构造的，不是采集的（本仓库不含真实威胁情报）。重跑结果逐字节一致。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "data" / "soc_intel.sqlite"

# (host, score, source)：只有 osint-demo 这一路会被 config/mcp.yaml 的那条 pull 取到
ROWS = [
    ("10.0.0.15", 4242, "osint-demo"),
    ("10.0.0.7", 310, "osint-demo"),
    ("10.0.0.5", 260, "osint-demo"),
    ("10.0.0.21", 180, "osint-demo"),
    ("10.0.0.99", 90, "osint-other"),      # 不在 pull 的 WHERE 里：证明过滤真的生效
    ("203.0.113.7", 77, "osint-demo"),
]


def build(out: Path = OUT) -> dict[str, int]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    try:
        conn.execute(
            "CREATE TABLE intel (host TEXT PRIMARY KEY, score INTEGER NOT NULL, source TEXT NOT NULL)"
        )
        conn.executemany("INSERT INTO intel(host, score, source) VALUES (?, ?, ?)", ROWS)
        conn.commit()
        pulled = conn.execute(
            "SELECT COUNT(*) FROM (SELECT host, score FROM intel WHERE source = 'osint-demo' ORDER BY host)"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"total_rows": len(ROWS), "pulled_rows": int(pulled)}


if __name__ == "__main__":
    stats = build()
    print(f"已写出 {OUT}：{stats['total_rows']} 行，其中 pull 可见 {stats['pulled_rows']} 行")
    sys.exit(0)
