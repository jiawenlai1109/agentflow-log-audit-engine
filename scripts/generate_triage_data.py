"""生成 M3 场景 S1 的三源演练数据（seed 固定，两次生成逐字节一致）。

三份文件刻意做成"跨源才答得出"的形状：

- `auth.csv`（防火墙/IAM 认证日志）：列名用 `src_ip`；
- `assets.csv`（资产台账）：列名用 `主机`，且**只有这里**有 `是否生产`；
- `edr.csv`（终端检测告警）：列名用 `主机`，只有这里有 `严重级`。

任何"生产域主机的异常登录"类判断都必须 join 两张表才能算出——这是 M3 要证的命题：
通用 agent 拿单表做不了，得靠包内列别名 + 跨表规则 + 派发前基数预检。

同时埋两类**误报陷阱**（规则不该命中它们，命中即说明规则写歪了）：
- 非生产域主机有大量失败（资产加权规则不得报）；
- 有 high 级 EDR 告警但认证日志干净的主机（跨表关联规则不得报）。
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

SEED = 42
OUT_DIR = Path(__file__).resolve().parents[1] / "demo" / "data" / "triage"
DAY = datetime(2026, 9, 5)

HOSTS = [f"10.0.0.{index}" for index in range(1, 25)]
PRODUCTION = HOSTS[::2]  # 12 台生产域主机
ACCOUNTS = [f"u{index:03d}" for index in range(1, 41)]

BRUTE_IP = "203.0.113.7"
BRUTE_ACCOUNT = "admin"
BRUTE_COUNT = 9  # ≥ 阈值 8
PROD_NOISY = "10.0.0.7"  # 生产域 + 多次失败 ⇒ T3 应命中
NONPROD_NOISY = "10.0.0.8"  # 非生产域 + 同样多次失败 ⇒ 误报陷阱，不得命中
EDR_CLEAN = "10.0.0.13"  # high 告警但认证干净 ⇒ 误报陷阱，不得命中
EDR_DIRTY = "10.0.0.15"  # high 告警 + 有失败 ⇒ T4 应命中


def _rows() -> list[dict[str, str]]:
    rng = random.Random(SEED)
    rows: list[list[object]] = []

    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    # 背景噪声：白天工作时段，成功率为主
    for _ in range(260):
        moment = DAY + timedelta(hours=rng.randint(8, 18), minutes=rng.randint(0, 59))
        rows.append(
            [
                stamp(moment),
                rng.choice(HOSTS),
                rng.choice(ACCOUNTS),
                "failed" if rng.random() < 0.12 else "success",
                rng.choice(["vpn", "bastion", "iam"]),
            ]
        )

    # T1 爆破：同一 (src_ip, account) 在 4 分钟内连续失败
    for index in range(BRUTE_COUNT):
        moment = DAY + timedelta(hours=20, seconds=10 * index)
        rows.append([stamp(moment), BRUTE_IP, BRUTE_ACCOUNT, "failed", "vpn"])

    # T3 生产域大量失败（应命中）与非生产域同样量失败（不得命中）
    for host, count in ((PROD_NOISY, 5), (NONPROD_NOISY, 5)):
        for index in range(count):
            moment = DAY + timedelta(hours=21, minutes=10 * index)
            rows.append([stamp(moment), host, rng.choice(ACCOUNTS), "failed", "bastion"])

    # T4 跨表关联：EDR 有 high 告警的主机同时出现失败认证
    for index in range(2):
        moment = DAY + timedelta(hours=22, minutes=7 * index)
        rows.append([stamp(moment), EDR_DIRTY, "svc_backup", "failed", "iam"])
    # 陷阱：这台主机只有 success 记录
    rows.append([stamp(DAY + timedelta(hours=22, minutes=40)), EDR_CLEAN, "u004", "success", "iam"])

    rows.sort(key=lambda row: str(row[0]))
    return [
        {
            "time": str(row[0]),
            "src_ip": str(row[1]),
            "account": str(row[2]),
            "auth_result": str(row[3]),
            "service": str(row[4]),
        }
        for row in rows
    ]


def _assets() -> list[dict[str, str]]:
    rng = random.Random(SEED + 1)
    return [
        {
            "主机": host,
            "域": "生产" if host in PRODUCTION else "测试",
            "责任人": f"责任人{index % 6 + 1}",
            "是否生产": "Y" if host in PRODUCTION else "N",
            "在线用户数": str(rng.randint(1, 30)),
        }
        for index, host in enumerate(HOSTS)
    ]


def _edr() -> list[dict[str, str]]:
    moment = (DAY + timedelta(hours=19)).strftime("%Y-%m-%d %H:%M:%S")
    alerts = [
        (EDR_DIRTY, "反弹 shell", "high"),
        (EDR_CLEAN, "挖矿进程", "high"),
        (PRODUCTION[3], "异常外联", "medium"),
        (HOSTS[9], "提权尝试", "low"),
    ]
    return [
        {"主机": host, "告警": name, "严重级": level, "时间": moment}
        for host, name, level in alerts
    ]


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "auth.csv": _rows(),
        "assets.csv": _assets(),
        "edr.csv": _edr(),
    }
    written = []
    for name, rows in outputs.items():
        path = out_dir / name
        _write(path, rows)
        written.append(path)
    return written


if __name__ == "__main__":
    for created in build():
        print(f"{created.name:12s} {created.stat().st_size:6d} 字节")
