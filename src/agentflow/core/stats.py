"""分布读数：**压测的尺子与上游探针必须共用一把**。

为什么单独一个模块（而不是各自在脚本里写一份）：P0/P2 的负载读数与 P4 的上游并发读数
要能放在一起比，前提是"p95"在两边是同一个算法算出来的。这个仓库已经为"同一件事两处实现"
付过几次学费（归属判据两份、配置值副本两份、执行缝隙两份），读数口径也一样。

两条口径写死在这里：

- **报分布，不报平均**。100 并发的故事全在尾部；平均值是这批里最容易被挑来骗人的那一个。
  `shape()` 仍然带 `mean` 字段，因为历史读数里有它，但它不许当判据用。
- **`n` 必须与分位数一起出现**。`n=5` 的"p95"其实就是"最慢的那一个"，
  不带 `n` 的 p95 是把抽样说成规律。
"""

from __future__ import annotations

import statistics
from typing import Sequence


def percentile(values: Sequence[float], q: float) -> float:
    """最近秩法（round 到整数位次）。空序列回 0.0——调用方要看 `n` 自己判断有没有意义。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q / 100 * (len(ordered) - 1)))))
    return ordered[index]


def shape(name: str, values: Sequence[float]) -> dict[str, float]:
    """一条分布的六个读数（含 `n`）。"""
    if not values:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
    }
