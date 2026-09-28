"""Bundle 上传与查看（#20）：一次多文件上传 = 一个数据快照。

与单文件 `/api/datasets` 的分工不是"新旧两套"：datasets 是历史接口，Bundle 才是
M2 之后输入的唯一真源。这里负责三件事——

1. **逐文件留痕**：每个文件成表、只作证据、还是被拒（为什么、怎么修），前端要能逐条显示；
2. **防线在解析之前**：扩展名与文件头双校验、zip 展开体积检查都在 `build_bundle` 之前跑，
   所以"没装可选依赖"不构成绕过路径；
3. **归属写进 SQL**：每一条读写都自带 `user_id` 谓词（M0 定下的纪律），不靠调用顺序兜。
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.config import (
    BUNDLES_DIR,
    MAX_BUNDLE_FILES,
    MAX_UPLOAD_MB,
    BUNDLE_ALLOWED_EXTENSIONS,
)
from app.db import execute, query, query_one
from app.deps import get_current_user
from app.upload_guard import scan_formula_cells, sniff_rejection, zip_bomb_rejection
from agentflow.core.bundle import Bundle, sha256_of
from agentflow.core.ingest import build_bundle

router = APIRouter(prefix="/api/bundles", tags=["bundles"])

# 异步解析专用：单线程，避免同时解析多个大包把内存顶穿；
# 与分析任务分开，是因为 JobManager 的两个 worker 是"跑分析"的预算，不该被解析占住
_PARSE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bundle-parse")

# 落到磁盘的文件名只允许这些字符（中文保留），其余替换为下划线：
# 用户给的文件名是外部输入，不能参与路径构造
_SAFE = re.compile(r"[^\w.\-\u4e00-\u9fff ]+")


def _safe_name(filename: str | None) -> str:
    raw = Path(filename or "unnamed").name
    cleaned = _SAFE.sub("_", raw).strip("._") or "unnamed"
    return cleaned[:80]


def _within(parent: Path, target: Path) -> bool:
    resolved = target.resolve()
    root = parent.resolve()
    return resolved == root or root in resolved.parents


@router.post("")
def create_bundle(
    files: list[UploadFile] | None = File(default=None),
    uploads: str | None = Form(default=None),
    async_parse: bool = Form(default=False),
    name: str | None = Form(default=None),
    user: dict = Depends(get_current_user),
) -> dict:
    """建包：`files` 直传与 `uploads`（分片重组）可以混用。

    顺序是刻意的：**分片重组在落 bundles 行之前**（它只会产生 4xx，失败不该留下永远
    `parsing` 的孤行）；**行落在解析之前**（解析可能在后台线程里崩，那时前端必须还查得到
    这个包，否则用户只看到"上传没反应"）。
    """
    files = files or []
    specs = _chunk_specs(uploads)
    if not files and not specs:
        raise HTTPException(status_code=400, detail="没有收到任何文件")
    if len(files) + len(specs) > MAX_BUNDLE_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_BUNDLE_FILES} 个文件")

    bundle_id = f"bu_{uuid.uuid4().hex[:12]}"
    display_name = name or bundle_id
    staging = BUNDLES_DIR / bundle_id / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    root = BUNDLES_DIR / bundle_id / "bundle"

    # 分片先重组：这一步只会产生 4xx（缺片/越权/改名），要在落 bundles 行之前做完，
    # 否则一次失败的重构会留下永远停在 parsing 的孤行
    assembled_files: list[tuple[str, Path, int, bool]] = []
    for spec in _chunk_specs(uploads):
        path, size, oversize = _assemble(staging, spec, user["id"])
        assembled_files.append((spec["filename"], path, size, oversize))

    _insert_bundle_row(bundle_id, user["id"], display_name, root)

    accepted: list[Path] = []
    records: list[dict[str, Any]] = []
    for index, upload in enumerate(files):
        target = staging / f"{index:03d}_{_safe_name(upload.filename)}"
        written, oversize = _write_limited(upload, target, MAX_UPLOAD_MB * 1024 * 1024)
        record = _guard_file(upload.filename or target.name, target, written, oversize)
        records.append(record)
        if record["kind"] == "pending":
            accepted.append(Path(record["stored_path"]))

    for filename, path, size, oversize in assembled_files:
        record = _guard_file(filename, path, size, oversize)
        records.append(record)
        if record["kind"] == "pending":
            accepted.append(Path(record["stored_path"]))

    if async_parse and accepted:
        # 分片重组后的包可能上百 MB，解析不能占住请求线程；用独立单线程池，
        # 不与分析任务抢 JobManager 的两个 worker
        _PARSE_POOL.submit(_finish_bundle, bundle_id, user["id"], display_name, root, records, accepted)
        return get_bundle_view(bundle_id, user)
    _finish_bundle(bundle_id, user["id"], display_name, root, records, accepted)
    return get_bundle_view(bundle_id, user)


def _guard_file(
    filename: str, stored: Path, written: int, oversize: bool
) -> dict[str, Any]:
    """单个已落盘文件的三道防线判断（直传与分片重组走同一段，不养出两份守卫）。"""
    suffix = Path(filename or "").suffix.lower()
    record: dict[str, Any] = {
        "filename": filename or stored.name,
        "stored_path": "" if (oversize or written == 0) else str(stored),
        "size": written,
        "sha256": "" if (oversize or written == 0) else sha256_of(stored)[:16],
        "kind": "skipped",
        "table_ref": None,
        "reason": None,
        "hint": None,
        "risk": {},
    }
    if oversize:
        record["reason"] = f"超过 {MAX_UPLOAD_MB}MB 上界（已丢弃，不在盘上留半份文件）"
        return record
    if written == 0:
        record["reason"] = "空文件（0 字节）"
        return record
    if suffix not in BUNDLE_ALLOWED_EXTENSIONS:
        record["reason"] = f"不支持的文件类型 {suffix or '(无扩展名)'}"
        stored.unlink(missing_ok=True)
        record["stored_path"] = ""
        return record
    with stored.open("rb") as handle:
        head = handle.read(8192)
    rejection = sniff_rejection(suffix, head)
    if rejection is None and suffix in {".xlsx", ".xlsm", ".zip"}:
        rejection = zip_bomb_rejection(stored)
    if rejection is not None:
        record["reason"] = rejection
        record["stored_path"] = ""
        stored.unlink(missing_ok=True)
        return record
    if suffix in {".csv", ".tsv", ".txt", ".log", ".md"}:
        record["risk"] = scan_formula_cells(stored)
    record["kind"] = "pending"
    return record


def _finish_bundle(
    bundle_id: str,
    user_id: int,
    name: str,
    root: Path,
    records: list[dict[str, Any]],
    accepted: list[Path],
) -> None:
    """解析 + 落库：同步路径与后台线程共用同一段代码，保证状态语义只有一套。"""
    bundle: Bundle | None = None
    error: str | None = None
    if accepted:
        try:
            bundle = build_bundle(accepted, root, name=name, strict=False)
        except Exception as exc:  # noqa: BLE001 - 整包失败要落成状态，不是抛给前端一个 500
            error = str(exc)[:400]
    if bundle is None:
        error = error or "没有可解析的文件：全部被拒（逐文件原因见 files）"
    elif not bundle.tables:
        error = error or "Bundle 里没有任何可分析的表：文本/日志只能作证据，数字必须来自表"
    _merge_parse_results(records, bundle)
    _store_result(bundle_id, user_id, root, records, bundle, error)


def _write_limited(upload: UploadFile, target: Path, limit: int) -> tuple[int, bool]:
    """边读边计字节、带上界落盘，返回 (写字节数, 是否超限)。

    先 `stat().st_size` 再判大的写法会在盘上留半份文件；这里超限即停并删，
    调用方拿到 `oversize` 后不再对该路径做任何读取（含 sha256）。
    """
    written = 0
    oversize = False
    with target.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                oversize = True
                break
            handle.write(chunk)
    if oversize:
        target.unlink(missing_ok=True)
    return written, oversize


# ---------------------------------------------------------------- 分片上传（#20）

# upload_id 由客户端生成，但必须像标识符：它参与路径构造
UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_CHUNKS = 200
CHUNKS_DIR_NAME = "_chunks"


@router.post("/chunks")
def upload_chunk(
    upload_id: str = Form(...),
    index: int = Form(..., ge=0),
    total: int = Form(..., ge=1, le=MAX_CHUNKS),
    filename: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
) -> dict:
    """一片分片：乱序、重复投递都允许，但**归属写进 manifest**。

    `upload_id` 是客户端给的字符串，所以它必须先被证明属于当前用户才能落盘——
    否则任何人拿别人的 upload_id 就能往对方的包里塞文件。
    """
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(status_code=400, detail="upload_id 只允许字母数字与 -_，长度 8~64")
    if index >= total:
        raise HTTPException(status_code=400, detail=f"分片序号 {index} 超出总分片数 {total}")
    directory = BUNDLES_DIR / CHUNKS_DIR_NAME / upload_id
    manifest_path = directory / "manifest.json"
    manifest = _read_manifest(manifest_path)
    if manifest is not None:
        if manifest["user_id"] != user["id"]:
            # 与跨用户资源一致的形状：一律 404，不承认这个 upload_id 存在
            raise HTTPException(status_code=404, detail="upload_id 不存在")
        if manifest["total"] != total or manifest["filename"] != filename:
            raise HTTPException(
                status_code=409,
                detail=f"同一 upload_id 不能改文件名或总分片数（已登记：{manifest['filename']} / {manifest['total']} 片）",
            )
    else:
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "upload_id": upload_id,
            "user_id": user["id"],
            "filename": filename,
            "total": total,
            "parts": {},
        }
    part = directory / f"{index:04d}.part"
    written, oversize = _write_limited(file, part, MAX_UPLOAD_MB * 1024 * 1024)
    if oversize:
        raise HTTPException(status_code=413, detail=f"单片超过 {MAX_UPLOAD_MB}MB 上界")
    manifest["parts"][str(index)] = written
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return {
        "upload_id": upload_id,
        "index": index,
        "received": sorted(int(key) for key in manifest["parts"]),
        "missing": _missing_indexes(manifest),
    }


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _missing_indexes(manifest: dict[str, Any]) -> list[int]:
    have = {int(key) for key in manifest.get("parts") or {}}
    return [i for i in range(int(manifest["total"])) if i not in have]


def _chunk_specs(uploads: str | None) -> list[dict[str, Any]]:
    if not uploads:
        return []
    try:
        specs = json.loads(uploads)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"uploads 不是合法 JSON：{exc}") from exc
    if not isinstance(specs, list):
        raise HTTPException(status_code=400, detail="uploads 必须是数组")
    out: list[dict[str, Any]] = []
    for item in specs:
        upload_id = str((item or {}).get("upload_id") or "")
        if not UPLOAD_ID.match(upload_id):
            raise HTTPException(status_code=400, detail=f"非法 upload_id：{upload_id!r}")
        out.append({"upload_id": upload_id, "filename": str(item.get("filename") or upload_id)})
    return out


def _assemble(staging: Path, spec: dict[str, Any], user_id: int) -> tuple[Path, int, bool]:
    """把分片按序拼成带原扩展名的文件；缺片直接拒，不"用已有的部分"凑一个假完整文件。"""
    upload_id = spec["upload_id"]
    directory = BUNDLES_DIR / CHUNKS_DIR_NAME / upload_id
    manifest = _read_manifest(directory / "manifest.json")
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"upload_id {upload_id} 没有登记过分片")
    # 归属从 manifest 里查，不看调用方怎么说：别人的 upload_id 拼不出任何东西
    if int(manifest["user_id"]) != int(user_id):
        raise HTTPException(status_code=404, detail=f"upload_id {upload_id} 没有登记过分片")
    if spec.get("filename") and manifest["filename"] != spec["filename"]:
        raise HTTPException(
            status_code=409,
            detail=f"upload_id {upload_id} 登记的文件名是 {manifest['filename']}，不能改名重组",
        )
    missing = _missing_indexes(manifest)
    if missing:
        raise HTTPException(
            status_code=400, detail=f"分片不完整，缺少序号 {missing}（共 {manifest['total']} 片）"
        )
    target = staging / f"asm_{_safe_name(manifest['filename'])}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    with target.open("wb") as handle:
        for index in range(int(manifest["total"])):
            part = directory / f"{index:04d}.part"
            data = part.read_bytes()
            total_bytes += len(data)
            if total_bytes > limit:
                handle.close()
                target.unlink(missing_ok=True)
                return target, total_bytes, True
            handle.write(data)
    shutil.rmtree(directory, ignore_errors=True)
    return target, total_bytes, False


def _merge_parse_results(records: list[dict[str, Any]], bundle: Bundle | None) -> None:
    """把 ingest 的逐文件结论并回上传记录：谁成了表、谁只是证据、谁为什么被跳过。"""
    if bundle is None:
        for record in records:
            if record["kind"] == "pending":
                record["kind"] = "skipped"
                record["reason"] = record["reason"] or "整包解析失败"
        return
    ref_by_stem = {Path(table.source_path).name: table.id for table in bundle.tables}
    doc_by_name = {doc.source_file: doc.id for doc in bundle.documents}
    skipped = {item.get("file"): item for item in bundle.skipped}
    for record in records:
        if record["kind"] != "pending":
            continue
        key = Path(record["stored_path"]).name
        item = skipped.get(key)
        if item is not None:
            record["kind"] = "skipped"
            record["reason"] = item.get("reason")
            record["hint"] = item.get("hint")
            continue
        table_ref = ref_by_stem.get(key)
        if table_ref is not None:
            record["kind"] = "table"
            record["table_ref"] = table_ref
            continue
        if key in doc_by_name:
            record["kind"] = "document"
            record["table_ref"] = doc_by_name[key]
            continue
        record["kind"] = "skipped"
        record["reason"] = "解析后未归入任何表或证据文件"


def _insert_bundle_row(bundle_id: str, user_id: int, name: str, root: Path) -> None:
    """先落一行 `parsing`：解析崩在后台线程里时，前端仍然查得到这个包和它的状态。"""
    execute(
        "INSERT INTO bundles (bundle_id, user_id, name, root, status, file_count) VALUES"
        " (?, ?, ?, ?, 'parsing', 0)",
        (bundle_id, user_id, name, str(root)),
    )


def _store_result(
    bundle_id: str,
    user_id: int,
    root: Path,
    records: list[dict[str, Any]],
    bundle: Bundle | None,
    error: str | None,
) -> None:
    status = "failed" if error else "ready"
    tables = list(bundle.tables) if bundle else []
    documents = list(bundle.documents) if bundle else []
    execute(
        "UPDATE bundles SET status = ?, error = ?, file_count = ?, table_count = ?,"
        " document_count = ? WHERE bundle_id = ? AND user_id = ?",
        (status, error, len(records), len(tables), len(documents), bundle_id, user_id),
    )
    # 重解析（同一 bundle_id 再入库）要先清干净，否则逐文件状态会累积成两份
    for child in ("bundle_tables", "bundle_files"):
        execute(f"DELETE FROM {child} WHERE bundle_id = ? AND user_id = ?", (bundle_id, user_id))
    for record in records:
        execute(
            "INSERT INTO bundle_files (bundle_id, user_id, filename, stored_path, size, sha256,"
            " kind, table_ref, reason, hint, risk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle_id,
                user_id,
                record["filename"],
                record["stored_path"],
                record["size"],
                record["sha256"],
                record["kind"],
                record["table_ref"],
                record["reason"],
                record["hint"],
                json.dumps(record.get("risk") or {}, ensure_ascii=False),
            ),
        )
    for table in tables:
        execute(
            "INSERT INTO bundle_tables (bundle_id, user_id, table_ref, source_file, path,"
            " row_count, columns, encoding, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle_id,
                user_id,
                table.id,
                table.source_file,
                str(table.path),
                int(table.row_count),
                json.dumps(list(table.columns), ensure_ascii=False),
                table.encoding,
                table.sha256,
            ),
        )


def _owned_bundle_row(bundle_id: str, user: dict[str, Any]) -> dict[str, Any]:
    row = query_one(
        "SELECT * FROM bundles WHERE bundle_id = ? AND user_id = ?", (bundle_id, user["id"])
    )
    if not row:
        # 不区分"不存在"与"别人的"：一律 404，不给枚举留缝
        raise HTTPException(status_code=404, detail="Bundle 不存在")
    return row


def get_bundle_view(bundle_id: str, user: dict[str, Any]) -> dict:
    row = _owned_bundle_row(bundle_id, user)
    files = query(
        "SELECT filename, size, sha256, kind, table_ref, reason, hint, risk"
        " FROM bundle_files WHERE bundle_id = ? AND user_id = ? ORDER BY id",
        (bundle_id, user["id"]),
    )
    for item in files:
        item["risk"] = json.loads(item.get("risk") or "{}")
    tables = query(
        "SELECT table_ref, source_file, row_count, columns, encoding, sha256"
        " FROM bundle_tables WHERE bundle_id = ? AND user_id = ? ORDER BY table_ref",
        (bundle_id, user["id"]),
    )
    for item in tables:
        item["columns"] = json.loads(item["columns"] or "[]")
    join_candidates: list[dict[str, Any]] = []
    if row["status"] == "ready" and Path(row["root"]).exists():
        try:
            join_candidates = Bundle.load(Path(row["root"])).join_candidates()
        except Exception:  # noqa: BLE001 - 候选是提示，读不出来不该让详情页 500
            join_candidates = []
    return {
        "bundle_id": bundle_id,
        "name": row["name"],
        "status": row["status"],
        "error": row["error"],
        "file_count": row["file_count"],
        "table_count": row["table_count"],
        "document_count": row["document_count"],
        "created_at": row["created_at"],
        "files": files,
        "tables": tables,
        "join_candidates": join_candidates,
    }


@router.get("")
def list_bundles(user: dict = Depends(get_current_user)) -> list[dict]:
    rows = query(
        "SELECT bundle_id, name, status, file_count, table_count, document_count, created_at"
        " FROM bundles WHERE user_id = ? ORDER BY id DESC",
        (user["id"],),
    )
    return rows


def load_bundle_for_analysis(bundle_id: str, user: dict[str, Any]) -> Bundle:
    """给 jobs 路由用：归属校验 + 状态校验 + 目录包含校验 + 载入快照，四步一处完成。

    放在本模块而不是 jobs.py，是因为它必须与本文件的写入用**同一个** `BUNDLES_DIR`——
    两处各自 import 的话，改一处配置就会让"自己写的快照"被判成越界文件。
    """
    row = _owned_bundle_row(bundle_id, user)
    if row["status"] != "ready":
        raise HTTPException(
            status_code=409, detail=f"Bundle 不可分析：{row['error'] or row['status']}"
        )
    root = Path(row["root"]).resolve()
    if not _within(BUNDLES_DIR, root) or not (root / "manifest.json").exists():
        raise HTTPException(status_code=410, detail="Bundle 文件已不在服务目录内，请重新上传")
    try:
        return Bundle.load(root)
    except Exception as exc:  # noqa: BLE001 - 快照损坏要显式拒绝，不能退化成"跑个别的"
        raise HTTPException(status_code=410, detail=f"Bundle 快照无法读取：{str(exc)[:200]}") from exc


@router.get("/{bundle_id}")
def read_bundle(bundle_id: str, user: dict = Depends(get_current_user)) -> dict:
    return get_bundle_view(bundle_id, user)


@router.get("/{bundle_id}/preview")
def preview_table(
    bundle_id: str,
    table_ref: str = Query(default="t1"),
    rows: int = Query(default=20, ge=1, le=200),
    user: dict = Depends(get_current_user),
) -> dict:
    """表预览：只读声明存在的那张表的前 N 行（数字来源仍是表，不是文档）。"""
    row = _owned_bundle_row(bundle_id, user)
    table = query_one(
        "SELECT * FROM bundle_tables WHERE bundle_id = ? AND table_ref = ? AND user_id = ?",
        (bundle_id, table_ref, user["id"]),
    )
    if table is None:
        raise HTTPException(status_code=404, detail=f"Bundle {bundle_id} 里没有表 {table_ref}")
    root = Path(row["root"]).resolve()
    target = Path(table["path"]).resolve()
    if not _within(root, target) or not target.exists():
        raise HTTPException(status_code=410, detail="表文件已不在该 Bundle 目录内，请重新上传")
    import pandas as pd

    frame = pd.read_csv(target, nrows=rows, encoding=table["encoding"] or "utf-8-sig")
    return {
        "bundle_id": bundle_id,
        "table_ref": table_ref,
        "source_file": table["source_file"],
        "row_count": table["row_count"],
        "columns": json.loads(table["columns"] or "[]"),
        "head": frame.astype(str).to_dict(orient="records"),
    }


@router.delete("/{bundle_id}")
def delete_bundle(bundle_id: str, user: dict = Depends(get_current_user)) -> dict:
    row = _owned_bundle_row(bundle_id, user)
    directory = BUNDLES_DIR / bundle_id
    if directory.exists() and _within(BUNDLES_DIR, directory):
        shutil.rmtree(directory, ignore_errors=True)
    for table in ("bundle_tables", "bundle_files", "bundles"):
        execute(f"DELETE FROM {table} WHERE bundle_id = ? AND user_id = ?", (bundle_id, user["id"]))
    return {"ok": True, "removed_root": str(Path(row["root"]).parent)}
