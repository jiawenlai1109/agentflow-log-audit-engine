"""生成多源评测夹具（#21）：三份小数据，形状分别对应三种判定。

seed 固定，两次生成逐字节一致（与 generate_soc_data.py 同风格）——评测集的题目必须
建立在不会漂移的数据上，否则 golden 核对测的是随机数而不是系统。

三份夹具各自要逼出的行为：
- `orders + hosts`：干净多对一（维表键唯一）→ 预检应放行、join 应真跑起来且三处同数；
- `calls_a + calls_b`：两侧同键都大量重复 → 期望行数 = 乘积，预检必须以 expansion 拦下；
- `runbook.md`：纯文本证据 → 只能进"证据"通道，报告里的数字不得来自它。
"""

from __future__ import annotations

import random
from pathlib import Path

SEED = 20260928
OUT_DIR = Path(__file__).resolve().parents[1] / "demo" / "data" / "multi"
DOMAINS = ("生产", "测试", "办公")


def build(out_dir: Path = OUT_DIR) -> list[Path]:
    rng = random.Random(SEED)
    out_dir.mkdir(parents=True, exist_ok=True)

    hosts = [f"h{i:02d}" for i in range(1, 21)]
    host_rows = [
        f"{host},{DOMAINS[index % len(DOMAINS)]},{1 if index % 3 else 0}"
        for index, host in enumerate(hosts)
    ]
    host_counts = {host: rng.randint(1, 4) for host in hosts}
    order_rows = [
        f"{host},{rng.randint(20, 400)},{2026 - (index % 3)}"
        for index, host in enumerate(hosts)
        for _ in range(host_counts[host])
    ]

    # 爆炸对：同一个会话号在两侧都重复 12 次 → inner join 期望 12×12=144 行。
    # 两侧都留一个数值列：否则"概况"任务会因为没有数值列而独立失败，
    # 这道题就变成在考两件不同的事（E19 要考的是"坏 join 会不会打死整条 run"）。
    explode_a = [f"c1,u{index % 4},{20 + index}" for index in range(12)]
    explode_b = [f"c1,{100 + index},x{index % 3}" for index in range(12)]

    files = {
        "orders.csv": "主机,事件数,年份\n" + "\n".join(order_rows) + "\n",
        "hosts.csv": "主机,域,是否生产\n" + "\n".join(host_rows) + "\n",
        "calls_a.csv": "会话,坐席,时长\n" + "\n".join(explode_a) + "\n",
        "calls_b.csv": "会话,次数,分机\n" + "\n".join(explode_b) + "\n",
        "runbook.md": (
            "# 处置手册（证据文件，不是数据）\n\n"
            "本手册示例值 987654.31 与 4242 只用于说明排版，任何统计口径都不应引用它们。\n"
        ),
    }
    written = []
    for name, text in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


if __name__ == "__main__":
    for created in build():
        print(f"{created.name:12s} {created.stat().st_size:6d} 字节")
