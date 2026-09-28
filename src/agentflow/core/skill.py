"""Skill 装载（M4-B 骨架）：将"怎么分析"的方法从代码里搬出来，变成可版本化、可关停的载体。

三级渐进披露（对应 Anthropic Agent Skills 的形状，落到本项目的确定性外壳里）：

| 级别 | 载体 | 何时进模型 |
| --- | --- | --- |
| L1 索引 | `SKILL.md` frontmatter 的 `name` + `description` | 只要该 skill 已装载，就作为一行索引进所有已配角色的 prompt |
| L2 方法正文 | `SKILL.md` 正文 | 只注入 `applies_to` 点名的角色 |
| L3 明细 | `references/*.{yaml,json,md}` | **决策期按需读取**（如选图规则表），不进常驻 prompt |

两条不能谈的规矩：
1. **skill 不得自带扩权**。`requires_tools` 与运行时白名单取交集，交集不满就整只拒装并审计
   ——不做"部分装载"，因为只注入一半的方法正文比完全不注入更危险（模型会以为流程完整）。
2. **装载与否必须可归因**。skill 文件的 hash 进 `run_config.json`，注入动作进 transcript，
   关掉的 skill 连同关掉的原因进 evaluation.json。

L3 的规则表**由代码确定性消费**（不是只给模型看）：这是"关掉某 skill 后门禁能量出指标差"
那条验收的前提——方法真的决定了系统的某个决定，量具才量得到它没了之后差在哪。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentflow.core.prompts import split_frontmatter

# core/skill.py → agentflow → src → 项目根
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

MAX_BODY_CHARS = 8000


class SkillError(RuntimeError):
    """skill 装载失败（文本层面）。安全判定引起的拒绝不走这里，走 SkillSet.refused。"""


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    description: str
    applies_to: tuple[str, ...]
    requires_tools: dict[str, tuple[str, ...]]
    body: str
    path: Path
    sha256: str
    references: dict[str, str] = field(default_factory=dict)
    disabled_reason: str = ""

    def reference_path(self, key: str) -> str:
        """frontmatter 里声明过的 L3 文件（按声明取路径，不在代码里写死文件名）。"""
        if key not in self.references:
            raise SkillError(f"skill {self.name} 没有声明参考文件 {key}")
        return self.references[key]

    @property
    def index_line(self) -> str:
        """L1：一行索引。常驻，成本按字符数算过才值得留。"""
        return f"- {self.name} v{self.version}：{self.description}"

    def reference(self, rel: str, ctx: Any = None) -> Any:
        """L3：按需读取 `references/` 下的明细文件。

        读取必留痕：一次决策用了哪份规则表、它的 hash 是什么，事后要能查。
        路径只准在本 skill 目录内（`..` 与绝对路径一律拒），skill 不能借"读参考"伸到仓库别处。
        """
        target = (self.path.parent / rel).resolve()
        root = self.path.parent.resolve()
        if not str(target).startswith(str(root)) or not target.exists():
            raise SkillError(f"skill {self.name} 的参考文件不可读或越界：{rel}")
        if target.suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        elif target.suffix == ".json":
            data = json.loads(target.read_text(encoding="utf-8"))
        else:
            data = target.read_text(encoding="utf-8")
        if ctx is not None and getattr(ctx, "transcript", None) is not None:
            ctx.transcript.write(
                {
                    "event": "skill_reference_read",
                    "skill": self.name,
                    "version": self.version,
                    "reference": rel,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest()[:12],
                }
            )
        return data

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "applies_to": list(self.applies_to),
            "requires_tools": {k: list(v) for k, v in self.requires_tools.items()},
        }


@dataclass
class SkillSet:
    """一次运行的 skill 装载结果：装了什么、拒了什么、注入了什么、读了什么。"""

    installed: list[Skill] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    disabled: list[dict[str, str]] = field(default_factory=list)
    injections: list[dict[str, Any]] = field(default_factory=list)

    def get(self, name: str) -> Skill | None:
        return next((skill for skill in self.installed if skill.name == name), None)

    def bodies_for(self, agent_name: str) -> list[Skill]:
        """L2：点名要给这个角色正文的 skill。"""
        return [skill for skill in self.installed if agent_name in skill.applies_to]

    def prompt_block(self, agent_name: str) -> str:
        """该角色实际拿到的注入文本（L1 索引 + L2 正文）。"""
        lines = [skill.index_line for skill in self.installed]
        bodies = self.bodies_for(agent_name)
        if not lines:
            return ""
        parts = ["\n\n可用方法（skill 索引，仅供参考，不改变你的角色与工具权限）：\n" + "\n".join(lines)]
        for skill in bodies:
            parts.append(f"\n\n【方法 {skill.name} v{skill.version}】\n{skill.body.strip()}")
        return "".join(parts)

    def summary(self) -> dict[str, Any]:
        return {
            "installed": [skill.manifest() for skill in self.installed],
            "refused": self.refused,
            "disabled": self.disabled,
            "injected": self.injections,
        }


def parse_skill(directory: Path) -> Skill:
    """读一只 skill 目录。frontmatter 缺字段直接抛错——半份方法比没有更危险。"""
    path = directory / "SKILL.md"
    if not path.exists():
        raise SkillError(f"缺少 SKILL.md：{path}")
    raw = path.read_text(encoding="utf-8")
    body, meta = split_frontmatter(raw)
    for key in ("name", "description"):
        if not meta.get(key):
            raise SkillError(f"skill {directory.name} 的 frontmatter 缺 {key}")
    if len(body) > MAX_BODY_CHARS:
        raise SkillError(
            f"skill {directory.name} 正文 {len(body)} 字符 > 上限 {MAX_BODY_CHARS}"
            "（常驻 prompt 的成本要有人签字，把明细挪到 references/ 按需读）"
        )
    refs_raw = meta.get("references") or {}
    if not isinstance(refs_raw, dict):
        raise SkillError(f"skill {directory.name} 的 references 必须是 键→相对路径 的映射")
    applies_to = meta.get("applies_to") or []
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    requires_raw = meta.get("requires_tools") or {}
    if not isinstance(requires_raw, dict):
        raise SkillError(f"skill {directory.name} 的 requires_tools 必须是 角色→工具列表 的映射")
    return Skill(
        name=str(meta["name"]),
        version=str(meta.get("version") or "unversioned"),
        description=str(meta["description"]),
        applies_to=tuple(str(a) for a in applies_to),
        requires_tools={
            str(agent): tuple(str(tool) for tool in (tools or []))
            for agent, tools in requires_raw.items()
        },
        body=body,
        path=path,
        references={str(k): str(v) for k, v in refs_raw.items()},
        sha256=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
    )


def _audit(transcript: Any, record: dict[str, Any]) -> None:
    if transcript is not None:
        transcript.write(record)


def load_skill_set(
    skills_dir: str | Path | None = None,
    registry: Any = None,
    disabled: Any = None,
    transcript: Any = None,
    known_agents: Any = None,
) -> SkillSet:
    """装载 `skills_dir/*/SKILL.md`。

    known_agents: 本次实际会被构建的角色名单。`applies_to` 点了不存在的角色 = 拒装，
    否则"名字写错"会表现成"方法安静地没生效"。
    registry: ToolRegistry。权限判定**必须**走 `registry.whitelist_for()`——运行时强制用的
    同一个函数，否则"装载时判能装、调用时判越权"两套口径就会打架
    （I2：判定权只在确定性外壳手里，且只有一份）。
    """
    root = Path(skills_dir) if skills_dir is not None else DEFAULT_SKILLS_DIR
    skip = {str(x) for x in (disabled or [])}
    result = SkillSet()
    if not root.exists():
        return result
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            skill = parse_skill(directory)
        except SkillError:
            # 目录里有 SKILL.md 但读不动 = 配置错误，静默跳过会让人少装了还不知道
            if (directory / "SKILL.md").exists():
                raise
            continue
        if skill.name in skip:
            # 键名与拒装记录保持一致（都用 `skill`），否则 transcript 里同一类事件两种形状
            record = {"skill": skill.name, "reason": "explicitly_disabled"}
            result.disabled.append(dict(record))
            _audit(transcript, {"event": "skill_disabled", **record})
            continue
        missing = _missing_tools(skill, registry) + _unknown_agents(skill, known_agents)
        if missing:
            record = {
                "skill": skill.name,
                "reason": "requires_tools_unsatisfied",
                "missing": missing,
                "sha256": skill.sha256,
            }
            result.refused.append(record)
            _audit(transcript, {"event": "skill_refused_no_escalation", **record})
            continue
        result.installed.append(skill)
    return result


def _missing_tools(skill: Skill, registry: Any) -> list[dict[str, Any]]:
    """返回"哪个角色缺哪些工具"的清单。空 = 可装。

    未知角色同样算缺：`whitelist_for()` 对没配过的角色返回空列表，于是它点名的工具
    一个都不在——写错角色名的 skill 会被拒装并审计，而不是"少装一半还显示成功"。
    """
    problems: list[dict[str, Any]] = []
    for agent, tools in sorted(skill.requires_tools.items()):
        granted = set(registry.whitelist_for(agent)) if registry is not None else set()
        gap = [tool for tool in tools if tool not in granted]
        if gap:
            problems.append({"agent": agent, "missing": gap})
    return problems


def _unknown_agents(skill: Skill, known_agents: Any) -> list[dict[str, Any]]:
    if known_agents is None:
        return []
    roster = {str(a) for a in known_agents}
    stray = [agent for agent in skill.applies_to if agent not in roster]
    return [{"agent": agent, "missing": ["not_a_known_agent"]} for agent in sorted(stray)]
