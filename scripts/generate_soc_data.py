"""生成 SOC 多源演练数据（M2/M3 用，seed 固定可复现）。

三份异构输入，刻意造出真实审计现场的样子：

- `firewall.csv`：认证日志（列名是英文 `src_ip`），含 1 组植入的爆破痕迹；
- `assets.tsv`：资产台账（列名是中文 `主机`），带"是否生产"域标记；
- `edr.json`：EDR 告警（同中文键 `主机`）。

**列名故意不统一**（src_ip vs 主机）：跨表关联要靠场景包的 data_convention 做列映射，
这不是数据缺陷，是要测的能力。t2/t3 之间则有真实同名列，join 预检应当命中。
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "demo" / "data" / "soc"
SEED = 42

# 植入攻击：与 login_audit 包的 R1/R2 语义对齐，便于 M3 直接复用规则
BRUTE_IP = "203.0.113.7"
SPRAY_IP = "45.33.32.156"
OFFHOURS_ACCOUNT = "svc_backup"


def _hosts(count: int = 24) -> list[str]:
    return [f"10.0.{i // 256}.{i % 256}" for i in range(1, count + 1)]


def build_firewall(hosts: list[str], accounts: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for _ in range(600):
        rows.append(
            {
                "time": f"2026-09-05 {random.randint(0, 23):02d}:{random.randint(0, 59):02d}:{random.randint(0, 59):02d}",
                "src_ip": random.choice(hosts),
                "account": random.choice(accounts),
                "auth_result": random.choice(["success", "failed", "failed"]),
                "service": random.choice(["vpn", "ssh", "webmail"]),
            }
        )
    # R1 爆破：同一 IP 短时间大量失败
    for minute in range(6):
        rows.append(
            {
                "time": f"2026-09-05 09:{55 + minute:02d}:00",
                "src_ip": BRUTE_IP,
                "account": "admin",
                "auth_result": "failed",
                "service": "vpn",
            }
        )
    # R2 爆破后成功
    rows.append(
        {
            "time": "2026-09-05 11:12:00",
            "src_ip": "198.51.100.23",
            "account": "backup_admin",
            "auth_result": "success",
            "service": "ssh",
        }
    )
    # R3 非常规时段成功
    for stamp in ("03:15:00", "03:40:00"):
        rows.append(
            {
                "time": f"2026-09-05 {stamp}",
                "src_ip": "10.0.5.66",
                "account": OFFHOURS_ACCOUNT,
                "auth_result": "success",
                "service": "vpn",
            }
        )
    # R4 口令喷洒：一个 IP 打多个账号
    for index in range(6):
        rows.append(
            {
                "time": f"2026-09-05 14:0{index}:00",
                "src_ip": SPRAY_IP,
                "account": f"spray{index}",
                "auth_result": "failed",
                "service": "webmail",
            }
        )
    return rows


def main() -> int:
    random.seed(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hosts = _hosts()
    accounts = [f"u{i:03d}" for i in range(1, 41)]

    firewall = build_firewall(hosts, accounts)
    with (OUT_DIR / "firewall.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(firewall[0].keys()))
        writer.writeheader()
        writer.writerows(firewall)

    with (OUT_DIR / "assets.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["主机", "域", "责任人", "是否生产"])
        for host in hosts:
            writer.writerow(
                [host, "生产" if random.random() < 0.3 else "测试", f"责任人{random.randint(1, 6)}", "N"]
            )

    edr = [
        {
            "主机": random.choice(hosts),
            "告警": random.choice(["反弹 shell", "挖矿进程", "无异常", "异常外联"]),
            "严重级": random.choice(["high", "medium", "low"]),
        }
        for _ in range(50)
    ]
    (OUT_DIR / "edr.json").write_text(json.dumps(edr, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"已生成 {OUT_DIR}：firewall.csv {len(firewall)} 行 / assets.tsv {len(hosts)} 行 / edr.json {len(edr)} 条")
    print("植入攻击：R1 爆破（203.0.113.7→admin）、R2 爆破后成功、R3 非常规时段、R4 口令喷洒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
