"""产物在哪：把 run_id / 会话行 换成磁盘上的位置。

为什么单独一个模块：**"谁能看见"与"文件在哪"必须同时答，但权威各自只有一份**。
归属判据在 `app/access.py`，目录推导在 `app/config.py`，这里只是把两条接起来——
如果定位逻辑散在 media/reports/sessions 三个路由里，那"换一棵企业树"就要改三处，
而漏掉任何一处的表现不是报错，是**读不到自己该读的东西**或者**读到了别人的**。

三条口径：

1. **先判可见，再给位置**。`run_dir` 里那条查询带归属过滤（`access.scope`），看不见就
   直接 404——反过来"先取到路径、再想起来判权限"的形状，迟早有人只走前一半。
2. **URL 里没有企业段**。产物地址仍是 `/outputs/<run_id>/<文件>`，org 段只存在于服务器
   本地路径里。把 org_id 拼进 URL 等于把内部编号与存储布局一起交给客户端，而它对使用者
   没有任何意义（这是 #15 撤掉 `report_path` 时用的同一条判据）。
3. **命名空间之前的历史产物仍然可读**：企业树里没有就看 `$OUTPUTS_ROOT/<run_id>`。
   能走到这一步说明可见性已经判过，所以这条回退不新增任何放行路径。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from agentflow.core.tools import ensure_within
from app import access, config

# run_id 的生成规则（`core/context.py:new_run_id`）：进文件路径前先按它收紧，
# 杜绝 `..`、绝对路径与任意段。地址与列表都用这一份，不再有第二份正则。
RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{8}$")

NOT_FOUND = "资源不存在"


def validate_run_id(run_id: str) -> str:
    """run_id 来自 URL：形状不对一律按"不存在"收敛，不给探测方区分"错在哪"的信息。"""
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return run_id


def resolve(org_id: int, run_id: str) -> Path:
    """已知归属时的定位：先看企业树，再看命名空间之前的老位置。

    两边都不存在时回**规范位置**（企业树里的那个），不回老位置——目录不存在本来就会由
    调用方的 `exists()` / `is_file()` 收敛成 404，而回一个"看起来更旧"的位置会让将来
    重跑同名 run 的行为变得说不清。
    """
    current = config.org_outputs_root(org_id) / run_id
    if current.is_dir():
        return current
    legacy = config.outputs_root() / run_id
    if legacy.is_dir():
        return legacy
    return current


def run_dir(run_id: str, user: dict[str, Any]) -> Path:
    """run_id → 产物目录，授权与定位在同一步里做完（判据只有 access 那一份）。"""
    org_id = access.visible_run_org(run_id, user)
    if org_id is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return resolve(org_id, validate_run_id(run_id))


def run_dir_for_row(row: dict[str, Any], run_id: str) -> Path:
    """已经按归属过滤取回的行（列表页就属于这一类）：直接用行里的 `org_id` 定位。

    这里**不再判一次可见性**——行是从带 `access.scope` 的查询里出来的，再判一遍就是给
    同一件事写第二条查询，而两条查询之间状态会动。
    """
    return resolve(int(row.get("org_id") or access.UNASSIGNED_ORG), run_id)


def session_dir(row: dict[str, Any]) -> Path:
    """会话目录按**会话行**的企业归属定位，不是按发起那次运行的作业行。

    两者的区别不是巧合：会话是个人上下文（OWNER 判据），运行产物是企业共享资产（SHARED
    判据），它们各自的归属列各自决定自己那棵树。让引擎去猜（原来是
    `<它拿到的 outputs_root>/sessions/<id>`）就会多出第二条推导式——一个人换了企业之后
    续轮，两条式子算出两个目录，表现是"这个会话的记忆忽然空了"，而且没有任何一处报错。
    """
    root = config.sessions_root(int(row.get("org_id") or access.UNASSIGNED_ORG))
    return _inside(root, str(row["session_id"]))


def _inside(root: Path, relative: str) -> Path:
    """拼接结果必须还在自己那棵树里：越界与非法名字一并按 404 收敛，不外泄布局。"""
    try:
        return ensure_within(root, relative)
    except Exception as exc:  # noqa: BLE001 - 非法路径与越界不作区分
        raise HTTPException(status_code=404, detail=NOT_FOUND) from exc


def media_target(run_id: str, rel_path: str, user: dict[str, Any]) -> Path:
    """产物文件：先按归属定到 run 目录，再在目录内解析相对路径（越界 404）。"""
    return _inside(run_dir(run_id, user), rel_path)
