"""场景包（pack）目录接口：让 Web 侧知道有哪些领域场景可跑、以及哪些装不起来。

只有两件事：列包、把包名变成可用信息。运行仍然走 `/api/analyze`（带 `pack` 字段），
这里不开第二条运行通道——两条入口的运行语义迟早分叉，本项目已经为此付过一次学费
（`DatasetsView` 那条写死 mock 的"追问"）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.deps import get_current_user
from agentflow.core.pack import list_packs

router = APIRouter(prefix="/api", tags=["packs"])


def _view(pack: Any) -> dict[str, Any]:
    return {
        "name": pack.name,
        "version": pack.version,
        "description": pack.description,
        "subject_label": pack.subject_label,
        "rule_count": len(pack.rules),
        "rule_ids": [rule.id for rule in pack.rules],
        "required_columns": list(pack.required_columns),
        "report_sections": list(pack.report_sections),
    }


@router.get("/packs")
def packs(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """可用场景包 + 装载失败的目录（带原因）。

    坏目录**不静默消失**：一个 rules.yaml 少了一行的包，如果从列表里蒸发，
    用户看到的就是"没有这个场景"，而真因在磁盘上——那是最难自查的一类分歧。
    需要登录才给这个列表：包名与必需列合起来就是系统能力面的地图。
    """
    loaded, broken = list_packs()
    return {"packs": [_view(pack) for pack in sorted(loaded, key=lambda p: p.name)], "broken": broken}
