"""上传防线（#20）：扩展名之外的第二只眼睛。

三道判断都是确定性的、可在写盘前后分别执行，因此单独成模块（不塞进 router）：

1. **内容嗅探**：扩展名声明"是什么"，文件头字节证明"确实是"。表格类容器（xlsx 是 zip、
   parquet 有魔数、老 xls 是 OLE2 复合文档）必须有对应魔数；文本类（csv/tsv/json/log…）
   反过来要求**不得**命中任何二进制魔数，且能在编码探测下解码。
2. **解压比**：xlsx/zip 这类容器可以在 50MB 里装出几 GB 的展开体积。用 stdlib `zipfile`
   在**任何解析器接触它之前**把展开字节数与压缩比算出来——这一步不依赖 openpyxl，
   所以"没装可选依赖"不能作为绕过理由。
3. **公式注入**：以 `=`/`+`/`-`/`@` 开头的单元格在电子表格软件里会被当公式执行。这里
   **只标记不改数据**：改写会让 `sources/` 里的原件与 sha256 失去"当时读的就是这一份"
   的意义（I1 数字责任链）。要中和的地方是"往电子表格导出"的那一刻（M5）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

# 二进制容器魔数：扩展名 → 必需的文件头前缀
MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06"),
    ".xlsm": (b"PK\x03\x04", b"PK\x05\x06"),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".parquet": (b"PAR1", b"PAND"),
    ".zip": (b"PK\x03\x04",),
}
# 已知二进制格式全集：文本类扩展名命中任何一个都直接拒（只看"像不像二进制"，
# 不要求与声明扩展名相符——伪装成 .csv 的 PNG 同样进不了文本解析分支）
BINARY_MAGICS: dict[bytes, str] = {
    b"\x89PNG": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"GIF8": "GIF",
    b"%PDF": "PDF",
    b"PK\x03\x04": "ZIP",
    b"\x1f\x8b": "GZIP",
    b"BZh": "BZIP2",
    b"\xfd7zXZ": "XZ",
    b"7z\xbc\xaf\x27\x1c": "7Z",
    b"Rar!": "RAR",
    b"\x7fELF": "ELF",
    b"MZ": "Windows PE",
    b"\xd0\xcf\x11\xe0": "OLE2 复合文档",
    b"PAR1": "Parquet",
    b"SQLite format 3": "SQLite",
}
# 文本类扩展名：不得命中上面任何魔数，且必须可解码
TEXT_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".txt",
    ".log",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".ndjson",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".conf",
}
SNIFF_BYTES = 8192

# 解压防线：容器展开后最多 200MB；压缩比只在"展开体积也已经不小"时才作为判据
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0
# 小表高度可压缩是正常的（重复文本能压到几百比一）；只有"又大又炸"才拦
RATIO_MIN_UNCOMPRESSED = 20 * 1024 * 1024

# 公式注入的触发前缀（电子表格软件会当公式执行）
FORMULA_PREFIXES = ("=", "+", "-", "@")


def sniff_rejection(suffix: str, head: bytes) -> str | None:
    """扩展名声明与文件头字节不一致时给出拒绝理由（一致则返回 None）。"""
    suffix = suffix.lower()
    if suffix in MAGIC_PREFIXES:
        if not any(head.startswith(magic) for magic in MAGIC_PREFIXES[suffix]):
            return f"扩展名 {suffix} 声明的是二进制表格容器，但文件头不匹配其魔数（可能被改名伪装）"
        return None
    if suffix in TEXT_EXTENSIONS:
        hit = next(((magic, name) for magic, name in BINARY_MAGICS.items() if head.startswith(magic)), None)
        if hit is not None:
            return (
                f"扩展名 {suffix} 声明的是文本，但文件头是 {hit[1]}（{hit[0]!r}）——"
                "拒绝按文本解析，也不猜测真实类型"
            )
    return None


def zip_bomb_rejection(path: str | Path) -> str | None:
    """在解析器接触容器之前算展开体积（stdlib zipfile，不需要 openpyxl）。"""
    target = Path(path)
    if not zipfile.is_zipfile(target):
        return None  # 非 zip 容器（如老 xls）由魔数与大小上界负责
    try:
        with zipfile.ZipFile(target) as archive:
            infos = archive.infolist()
            uncompressed = sum(int(info.file_size) for info in infos)
            compressed = sum(int(info.compress_size) for info in infos) or 1
    except (OSError, zipfile.BadZipFile) as error:
        return f"zip 容器无法读取：{str(error)[:120]}"
    if uncompressed > MAX_UNCOMPRESSED_BYTES:
        return (
            f"容器展开后约 {uncompressed // (1024 * 1024)}MB，超过上界 "
            f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB"
        )
    ratio = uncompressed / compressed
    # 只在"又大又炸"时按比值拦：小表重复文本压到几百比一是正常现象，不是攻击
    if uncompressed > RATIO_MIN_UNCOMPRESSED and ratio > MAX_COMPRESSION_RATIO:
        return (
            f"展开 {uncompressed // (1024 * 1024)}MB、压缩比 {ratio:.1f}:1 超过上界 "
            f"{MAX_COMPRESSION_RATIO:.0f}:1（疑似解压炸弹）"
        )
    return None


def scan_formula_cells(path: str | Path, limit: int = 5000) -> dict[str, Any]:
    """统计"会被当公式执行"的单元格（只计数与取样，不改数据）。

    负数是合法数值（退款就是负值），所以按前缀粗筛之后还要排除纯数值——否则每份带亏损
    的数据都被报成注入风险，这条指标很快就没人信了。
    """
    import csv

    from agentflow.core.bundle import detect_encoding

    target = Path(path)
    try:
        with target.open(encoding=detect_encoding(target), newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return {"formula_cells": 0, "samples": [], "scanned_rows": 0}
    suspicious = [
        cell
        for row in rows[:limit]
        for cell in row
        if cell[:1] in FORMULA_PREFIXES and not _looks_numeric(cell)
    ]
    return {
        "formula_cells": len(suspicious),
        "samples": [cell[:40] for cell in suspicious[:3]],
        "scanned_rows": min(len(rows), limit),
    }


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True
