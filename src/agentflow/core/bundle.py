"""Bundle：一次运行的输入契约（取代贯穿全栈的单个 `data_path`）。

三条不可让的语义，都写在这里而不是散落在各 Agent：

1. **表与文档分流**（I1 数字责任链的地基）：能归一化成二维表的文件进 `tables`，是分析
   与校验的唯一数字来源；文本/日志/图片进 `documents`，只能被引用作证据，
   永远不会出现在可被聚合的表清单里；
2. **原件不可变**：上传的文件一律复制进 `sources/` 并记 sha256，归一化产物另存
   `tables/<id>.csv`。校验器重放时读的是归一化表，证据引用指向原件副本；
3. **自描述**：`manifest.json` 落全量元数据，Bundle 可从目录独立重建，
   这样报告里的任何数字都能回答"你读的是哪一份、它当时的 sha256 是多少"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
TABLES_DIR = "tables"
SOURCES_DIR = "sources"

# 编码探测顺序：中文 Windows 导出的日志/CSV 常见 gbk/gb18030，BOM 优先
ENCODING_CANDIDATES = ("utf-8-sig", "gbk", "gb18030", "utf-8")

# 归一化上界：单表行数与单文件字节数，超了直接拒——宁可拒绝也不要 OOM
MAX_TABLE_ROWS = 2_000_000
MAX_FILE_BYTES = 200 * 1024 * 1024

# join 候选键的取值重叠率下界：低于这个比例，两表其实连不上
JOIN_OVERLAP_FLOOR = 0.5
JOIN_SAMPLE_VALUES = 2000


class BundleError(ValueError):
    """契约级错误：Bundle 无法构成或被正确加载。"""


def detect_encoding(path: Path) -> str:
    for encoding in ENCODING_CANDIDATES:
        try:
            path.read_text(encoding=encoding)
            return encoding
        except (UnicodeDecodeError, OSError):
            continue
    return "utf-8"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def canonical_column(column: str, aliases: dict[str, str] | None = None) -> str:
    """列的规范名：包内别名映射到规范名，没映射的沿用原名。"""
    if not aliases:
        return str(column)
    return str(aliases.get(str(column), column))


def column_for_canonical(
    columns: list[str], canonical: str, aliases: dict[str, str] | None = None
) -> str | None:
    """给定规范名，在这张表的实际列名里找出承载它的那一列。

    先精确命中再走别名：`主机` 这一列本身存在时不该被别名规则改写语义。
    """
    if canonical in columns:
        return canonical
    for actual, target in (aliases or {}).items():
        if target == canonical and actual in columns:
            return actual
    return None


def _shared_keys(
    left: "Table", right: "Table", aliases: dict[str, str] | None
) -> list[tuple[str, str, str]]:
    """两张表可用的连接键：返回 (规范名, 左表实际列, 右表实际列)。

    跨源数据最常见的形状就是"同一实体三种叫法"（`src_ip` / `主机` / `host`）。
    只认同名会漏掉全部跨表规则；认别名又必须按列名匹配，因为表 id 取决于上传顺序。
    """
    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for left_column in left.columns:
        canonical = canonical_column(left_column, aliases)
        right_column = column_for_canonical(right.columns, canonical, aliases)
        if right_column is None:
            continue
        triple = (canonical, left_column, right_column)
        if triple not in seen:
            seen.add(triple)
            pairs.append(triple)
    return pairs


@dataclass
class Table:
    """一张可分析的表：原件引用 + 归一化 CSV + 结构元数据。"""

    id: str
    source_file: str
    source_path: str
    path: str
    row_count: int
    columns: list[str]
    encoding: str
    sha256: str
    sheet: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Document:
    """一份证据文档：可被引用，不可被聚合（I1）。"""

    id: str
    source_file: str
    path: str
    sha256: str
    size: int
    preview: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Bundle:
    """一次运行的全部输入。"""

    id: str
    root: Path
    tables: list[Table] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_files: list[str] = field(default_factory=list)
    # 宽松入包时被拒的文件与原因（Web 上传要让操作者看见"哪一张没进来"）
    skipped: list[dict[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------ 访问

    @property
    def primary(self) -> Table:
        """主表：兼容"只有一张表"的既有语义（单文件上传、报告默认数据源）。

        这不是向后兼容的补丁——单文件本就是"只有一个成员的 Bundle"，
        派生属性比再存一份 data_path 更不容易走样。
        """
        if not self.tables:
            raise BundleError("Bundle 里没有可分析的表（文档不能当数字来源）")
        return self.tables[0]

    def table(self, ref: str) -> Table:
        for item in self.tables:
            if item.id == str(ref) or item.source_file == str(ref):
                return item
        raise BundleError(f"Bundle 中不存在表 {ref}；可用：{[t.id for t in self.tables]}")

    def readable_paths(self) -> set[Path]:
        """运行期允许被读的文件：归一化表 + 原件副本（证据引用）。"""
        paths = {Path(t.path).resolve() for t in self.tables}
        paths |= {Path(t.source_path).resolve() for t in self.tables}
        paths |= {Path(d.path).resolve() for d in self.documents}
        return paths

    def table_ids(self) -> list[str]:
        return [t.id for t in self.tables]

    def summary(self) -> str:
        parts = [f"{t.id}={t.source_file}({t.row_count}行×{len(t.columns)}列)" for t in self.tables]
        if self.documents:
            parts.append(f"文档 {len(self.documents)} 份（仅作证据，不作数字来源）")
        return "；".join(parts)

    # ------------------------------------------------------------------ 联表候选

    def join_candidates(self, aliases: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """确定性给出"哪两张表可能连得上"：同名列（或包内别名认定的同名列）+ 取值重叠率抽样。

        这里只做候选提示，不做放行——放行由任务的 dataset_refs 与 join 预检器决定。
        `aliases`（实际列名 → 规范名）来自场景包的 data_convention：跨源数据最常见的
        形状就是"同一个实体三种叫法"（`src_ip` / `主机` / `host`），只认同名会漏掉全部
        跨表规则；但别名只用于"认出来"，**不改写归一化 CSV 的列名**——原文可追是 I1 的地基。
        """
        out: list[dict[str, Any]] = []
        for index, left in enumerate(self.tables):
            for right in self.tables[index + 1 :]:
                for column, left_column, right_column in _shared_keys(left, right, aliases):
                    overlap = self._overlap(left, right, left_column, right_column)
                    if overlap is None:
                        continue
                    out.append(
                        {
                            "left": left.id,
                            "right": right.id,
                            "column": column,
                            "left_column": left_column,
                            "right_column": right_column,
                            "overlap": round(overlap, 3),
                            "usable": overlap >= JOIN_OVERLAP_FLOOR,
                        }
                    )
        return sorted(out, key=lambda item: item["overlap"], reverse=True)

    @staticmethod
    def _overlap(
        left: Table, right: Table, left_column: str, right_column: str | None = None
    ) -> float | None:
        """两表键列取值重叠率（左列取值中能在右列找到的比例）。"""
        right_column = right_column or left_column
        try:
            import pandas as pd

            left_values = pd.read_csv(
                left.path, usecols=[left_column], nrows=JOIN_SAMPLE_VALUES
            )[left_column]
            right_values = set(
                pd.read_csv(right.path, usecols=[right_column], nrows=JOIN_SAMPLE_VALUES)[
                    right_column
                ]
                .dropna()
                .astype(str)
            )
        except Exception:  # noqa: BLE001 - 读不动就当作无候选，不猜
            return None
        left_values = left_values.dropna().astype(str)
        if left_values.empty or not right_values:
            return None
        hits = sum(1 for value in left_values if value in right_values)
        return hits / len(left_values)

    # ------------------------------------------------------------------ 持久化

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source_files": self.source_files,
            "tables": [t.as_dict() for t in self.tables],
            "documents": [d.as_dict() for d in self.documents],
            "skipped": list(self.skipped),
        }

    def write(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / MANIFEST_NAME
        target.write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, root: str | Path) -> "Bundle":
        root = Path(root)
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.exists():
            raise BundleError(f"{root} 不是 Bundle（缺 {MANIFEST_NAME}）")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("tables",):
            if key not in data:
                raise BundleError(f"manifest 缺少 {key} 段")
        return cls(
            id=str(data.get("id") or root.name),
            root=root,
            tables=[Table(**entry) for entry in data["tables"]],
            documents=[Document(**entry) for entry in data.get("documents") or []],
            created_at=str(data.get("created_at") or ""),
            source_files=list(data.get("source_files") or []),
            skipped=list(data.get("skipped") or []),
        )

    def as_dict(self) -> dict[str, Any]:
        return self.manifest()
