"""ingest：把异构文件归一化成 Bundle。

设计约束：**运行期零新依赖**。所有表类输入在入包时落成归一化 CSV，之后的执行、
独立校验、join 预检一律只认 CSV——所以 xlsx / parquet 只在这一层出现，
缺 openpyxl / pyarrow 时给出可执行的安装提示，而不是让校验器模板也长出一堆分支。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from agentflow.core.bundle import (
    SOURCES_DIR,
    TABLES_DIR,
    Bundle,
    BundleError,
    Document,
    MAX_FILE_BYTES,
    MAX_TABLE_ROWS,
    Table,
    detect_encoding,
    sha256_of,
)

# 文档类输入：只作证据，永不进 tables
DOCUMENT_EXTENSIONS = {".txt", ".log", ".md", ".markdown", ".yaml", ".yml", ".xml", ".ini", ".conf"}
# 需要可选依赖的表类输入：扩展名 → (包名, 安装命令)
OPTIONAL_TABLE_READERS = {
    ".xlsx": ("openpyxl", "pip install openpyxl"),
    ".xlsm": ("openpyxl", "pip install openpyxl"),
    ".xls": ("xlrd", "pip install xlrd"),
    ".parquet": ("pyarrow", "pip install pyarrow"),
}

PREVIEW_CHARS = 2000


class IngestError(ValueError):
    """单个文件无法入包。带 hint 时说明是缺依赖，不是数据坏了。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def build_bundle(
    files: Iterable[str | Path],
    bundle_dir: str | Path,
    name: str | None = None,
    strict: bool = True,
) -> Bundle:
    """构造 Bundle。

    strict=True：任一文件不合法即整体失败（CLI 与分析入口用，避免"少了一张表"却继续算）；
    strict=False：坏文件记进 bundle.skipped（Web 上传用，用户要看到哪个文件为什么被拒）。
    """
    root = Path(bundle_dir).resolve()
    (root / SOURCES_DIR).mkdir(parents=True, exist_ok=True)
    (root / TABLES_DIR).mkdir(parents=True, exist_ok=True)

    bundle = Bundle(id=name or root.name, root=root)
    for original in files:
        path = Path(original)
        try:
            record = _ingest_one(bundle, path)
        except IngestError as error:
            if strict:
                # 保留 IngestError 类型与 hint：调用方要能区分"缺依赖"与"数据坏了"
                raise IngestError(f"{path.name}：{error}", hint=error.hint) from error
            bundle.skipped.append({"file": path.name, "reason": str(error), "hint": error.hint})
            continue
        if record["kind"] == "table":
            bundle.tables.append(record["value"])
        else:
            bundle.documents.append(record["value"])
        bundle.source_files.append(path.name)

    if strict and not bundle.tables:
        raise BundleError("Bundle 里没有任何可分析的表；纯文本/日志只能作证据文件")
    bundle.write()
    return bundle


def _ingest_one(bundle: Bundle, path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise IngestError("文件不存在")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise IngestError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上界")

    suffix = path.suffix.lower()
    source_target = _copy_to_sources(bundle, path)
    digest = sha256_of(source_target)

    if suffix in OPTIONAL_TABLE_READERS:
        package, install = OPTIONAL_TABLE_READERS[suffix]
        try:
            importlib_import(package)
        except ImportError as error:
            raise IngestError(f"读取 {suffix} 需要可选依赖 {package}", hint=install) from error
        frame = _read_excel_or_parquet(source_target, suffix)
        return {"kind": "table", "value": _store_table(bundle, frame, path.name, source_target, digest, encoding="n/a")}

    if suffix in {".csv", ".tsv"}:
        encoding = detect_encoding(source_target)
        frame = _read_csv(source_target, sep="\t" if suffix == ".tsv" else None, encoding=encoding)
        return {"kind": "table", "value": _store_table(bundle, frame, path.name, source_target, digest, encoding)}

    if suffix in {".json"}:
        encoding = detect_encoding(source_target)
        frame = _read_json(source_target, encoding)
        return {"kind": "table", "value": _store_table(bundle, frame, path.name, source_target, digest, encoding)}

    if suffix in {".jsonl", ".ndjson"}:
        encoding = detect_encoding(source_target)
        try:
            frame = _read_jsonl(source_target, encoding)
        except NotTabularError:
            return {"kind": "document", "value": _store_document(bundle, path.name, source_target, digest, size)}
        return {"kind": "table", "value": _store_table(bundle, frame, path.name, source_target, digest, encoding)}

    if suffix in DOCUMENT_EXTENSIONS:
        return {"kind": "document", "value": _store_document(bundle, path.name, source_target, digest, size)}

    raise IngestError(f"不支持的文件类型 {suffix or '(无扩展名)'}")


def importlib_import(package: str) -> None:
    import importlib

    importlib.import_module(package)


def _copy_to_sources(bundle: Bundle, path: Path) -> Path:
    target = bundle.root / SOURCES_DIR / path.name
    if target.resolve() != path.resolve():
        # 原件副本同样走"临时名 + rename"：读者拿到的一定是完整的一份原件
        _write_atomically(target, lambda staging: shutil.copyfile(path, staging))
    return target


def _guard_frame(frame: pd.DataFrame | None, label: str) -> pd.DataFrame:
    """空列 / 超上界直接拒；0 行但列齐全是合法输入（空结果语义反转要靠它）。"""
    if frame is None or len(frame.columns) == 0:
        raise IngestError(f"{label}：没有可用列")
    if len(frame) > MAX_TABLE_ROWS:
        raise IngestError(f"{label}：行数 {len(frame)} 超过上界 {MAX_TABLE_ROWS}")
    return frame


def _read_csv(path: Path, sep: str | None, encoding: str) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, sep=sep or ",", encoding=encoding)
    except (ValueError, UnicodeDecodeError, OSError) as error:
        raise IngestError(f"CSV 解析失败：{str(error)[:120]}") from error
    return _guard_frame(frame, path.name)


def _read_json(path: Path, encoding: str) -> pd.DataFrame:
    try:
        data = json.loads(path.read_text(encoding=encoding))
    except (ValueError, OSError, UnicodeDecodeError) as error:
        raise IngestError(f"JSON 解析失败：{str(error)[:120]}") from error
    if isinstance(data, list):
        if not data:
            raise IngestError("JSON 数组为空")
        if not all(isinstance(item, dict) for item in data):
            raise IngestError("JSON 数组元素必须都是对象，否则无法成表")
        frame = pd.DataFrame(data)
    elif isinstance(data, dict):
        columns = [value for value in data.values() if isinstance(value, list)]
        if columns and len({len(column) for column in columns}) == 1 and len(columns) == len(data):
            frame = pd.DataFrame(data)
        else:
            frame = pd.json_normalize(data)
    else:
        raise IngestError("顶层 JSON 不是对象或数组，无法成表")
    return _guard_frame(frame, path.name)


class NotTabularError(ValueError):
    """每行不是对象——按文档处理，不强行成表。"""


def _read_jsonl(path: Path, encoding: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding=encoding).splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except ValueError as error:
            raise NotTabularError(f"第 {len(rows) + 1} 行不是 JSON") from error
        if not isinstance(item, dict):
            raise NotTabularError("存在非对象行")
        rows.append(item)
    if not rows:
        raise IngestError("JSONL 为空")
    return _guard_frame(pd.DataFrame(rows), path.name)


def _read_excel_or_parquet(path: Path, suffix: str) -> pd.DataFrame:
    try:
        if suffix in {".parquet"}:
            return _guard_frame(pd.read_parquet(path), path.name)
        sheets = pd.read_excel(path, sheet_name=None)
        frames = [frame for frame in sheets.values() if not frame.empty]
        if not frames:
            raise IngestError("Excel 没有非空工作表")
        return _guard_frame(frames[0], path.name)
    except IngestError:
        raise
    except Exception as error:  # noqa: BLE001 - 可选依赖的失败形态多，统一成入包错误
        raise IngestError(f"解析失败：{str(error)[:120]}") from error


def _write_atomically(target: Path, writer: Callable[[Path], Any]) -> Path:
    """先写临时名、再整体 rename —— 读者只会看到"旧的完整一份"或"新的完整一份"。

    Bundle 缓存是跨 run 共享的目录，而归一化是好几个文件依次落盘。没有这一步时，
    并发冷启动的第二个请求可能读到"表文件已存在、内容只写了一半"的状态。
    rename 在同盘理论上是原子的；跨进程竞争由调用方的目录锁 + "manifest 最后写"兜住。
    """
    staging = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        writer(staging)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


def _store_table(
    bundle: Bundle, frame: pd.DataFrame, source_file: str, source_path: Path, digest: str, encoding: str
) -> Table:
    table_id = f"t{len(bundle.tables) + 1}"
    frame = frame.copy()
    frame.columns = [str(name).strip() for name in frame.columns]
    normalized = bundle.root / TABLES_DIR / f"{table_id}.csv"
    # utf-8-sig：让 Excel 与下游 pandas 都不用猜编码
    _write_atomically(
        normalized, lambda target: frame.to_csv(target, index=False, encoding="utf-8-sig")
    )
    return Table(
        id=table_id,
        source_file=source_file,
        source_path=str(source_path.resolve()),
        path=str(normalized.resolve()),
        row_count=int(len(frame)),
        columns=[str(name) for name in frame.columns],
        encoding=encoding,
        sha256=digest,
    )


def _store_document(bundle: Bundle, source_file: str, source_path: Path, digest: str, size: int) -> Document:
    document_id = f"d{len(bundle.documents) + 1}"
    encoding = detect_encoding(source_path)
    try:
        preview = source_path.read_text(encoding=encoding)[:PREVIEW_CHARS]
    except OSError:
        preview = ""
    return Document(
        id=document_id,
        source_file=source_file,
        path=str(source_path.resolve()),
        sha256=digest,
        size=int(size),
        preview=preview,
    )
