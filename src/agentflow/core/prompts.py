"""提示词装载：`prompts/<name>.md` 是各角色 system prompt 的唯一真源。

为什么要外置（M4 骨架第一条）：**可归因**。留在 .py 里的字符串只能靠"文件 hash"间接归因，
外置之后每个 prompt 有自己的版本号与 hash，分数变化能直接配对到某一份文本的 diff。

三条硬规矩：
1. 正文**逐字节**等于送进模型的那段文本——装载器不 strip、不加换行、不做模板渲染。
   文本一旦被加工，"hash 对应的是模型实际看到的东西"这个前提就没了。
2. 缺文件直接抛错，不退默认值。静默退默认 = 线上跑着一份没人核对过的 prompt。
3. frontmatter 只放元数据（agent / version / note），装载器把它整段剥掉。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

# core/prompts.py → agentflow → src → 项目根
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

# 七个角色 + 摘要器 + 数据内容防线
KNOWN_PROMPTS = (
    "explorer",
    "planner",
    "executor",
    "inspector",
    "visualizer",
    "reporter",
    "critic",
    "summarizer",
    "data_defense",
)


class PromptError(RuntimeError):
    """提示词装载失败。"""


def split_frontmatter(raw: str) -> tuple[str, dict[str, Any]]:
    """返回 (正文, 元数据)。正文从 frontmatter 结束后的第一个字节起，原样不动。"""
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return raw, {}
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return raw[match.end() :], meta


def read_prompt(name: str, prompts_dir: str | Path | None = None) -> tuple[str, dict[str, Any]]:
    """读一份提示词，返回 (正文, 元数据)。正文原样返回，不做任何加工。"""
    root = Path(prompts_dir) if prompts_dir is not None else DEFAULT_PROMPTS_DIR
    path = root / f"{name}.md"
    if not path.exists():
        raise PromptError(f"提示词文件缺失：{path}（宁可报错，不静默退回内置文本）")
    return split_frontmatter(path.read_text(encoding="utf-8"))


def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    return read_prompt(name, prompts_dir)[0]


def prompt_version(name: str, prompts_dir: str | Path | None = None) -> str:
    meta = read_prompt(name, prompts_dir)[1]
    return str(meta.get("version") or "unversioned")


def prompt_hashes(prompts_dir: str | Path | None = None) -> dict[str, str]:
    """全部提示词的 hash + 版本，供 run_config 落指纹。

    hash 取的是**正文**（模型实际收到的那段），不是文件字节——文件里的 frontmatter 改了
    而正文没改，行为没变，指纹也就不该变。版本单列一栏，两件事分开归因。
    """
    out: dict[str, str] = {}
    for name in KNOWN_PROMPTS:
        try:
            body, _meta = read_prompt(name, prompts_dir)
        except PromptError:
            out[f"prompt:{name}"] = "missing"
            continue
        out[f"prompt:{name}"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return out
