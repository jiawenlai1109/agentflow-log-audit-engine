"""生成固定验收数据集（确定性 seed，便于回归验证 TC-01/02/03）。

产物：
  demo/data/retail_sales.csv            # 无"利润"列（用于 TC-03 缺失字段场景）
  demo/data/retail_sales_with_profit.csv  # 含"利润"列
"""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd


def make_retail_sales(seed: int = 42, n: int = 2000, with_profit: bool = False) -> pd.DataFrame:
    rng = random.Random(seed)
    categories = ["电子产品", "服装", "食品", "家居", "美妆"]
    regions = ["华东", "华南", "华北", "西南", "东北"]
    segments = ["企业客户", "个人客户", "新客", "老客"]
    dates = pd.date_range("2024-01-01", "2024-07-31", freq="D")

    rows = []
    for _ in range(n):
        date = dates[rng.randrange(len(dates))]
        negative = rng.random() < 0.02  # 约 2% 退款（负销售额）
        amount = round(rng.uniform(20, 2000) * (-1 if negative else 1), 2)
        row = {
            "订单日期": date.strftime("%Y-%m-%d"),
            "产品类别": rng.choice(categories),
            "销售额": amount,
            "销量": rng.randint(1, 50),
            "地区": rng.choice(regions),
            "客户细分": rng.choice(segments),
        }
        if with_profit:
            row["利润"] = round(amount * rng.uniform(0.05, 0.35), 2)
        if rng.random() < 0.01:  # 制造少量缺失值
            key = rng.choice(list(row))
            row[key] = None
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("订单日期").reset_index(drop=True)
    return df


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "demo" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    make_retail_sales(seed=42).to_csv(
        out_dir / "retail_sales.csv", index=False, encoding="utf-8-sig"
    )
    make_retail_sales(seed=7, with_profit=True).to_csv(
        out_dir / "retail_sales_with_profit.csv", index=False, encoding="utf-8-sig"
    )
    print(f"已生成到：{out_dir}")
    for file in sorted(out_dir.glob("*.csv")):
        frame = pd.read_csv(file)
        print(f"  {file.name}: {len(frame)} 行, 列={list(frame.columns)}")


if __name__ == "__main__":
    main()
