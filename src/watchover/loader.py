"""Universal loader: file / ZIP / GZ / TAR.GZ / directory -> (name, text) pairs with encoding fallback."""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile
from pathlib import Path
from typing import Iterator

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".parquet", ".xlsx", ".xls", ".db", ".sqlite", ".pkl", ".so"}
ENCODINGS = ("utf-8-sig", "utf-16", "latin-1")


def decode(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return None
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def iter_bytes(name: str, data: bytes) -> Iterator[tuple[str, str]]:
    lower = name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir() and not info.filename.startswith("__MACOSX"):
                    yield from iter_bytes(info.filename, zf.read(info))
        return
    if lower.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    yield from iter_bytes(m.name, tf.extractfile(m).read())
        return
    if lower.endswith(".gz"):
        yield from iter_bytes(name[:-3], gzip.decompress(data))
        return
    if Path(lower).suffix in BINARY_EXT:
        return
    text = decode(data)
    if text is not None:
        yield name, text


def iter_path(path: str | Path) -> Iterator[tuple[str, str]]:
    path = Path(path)
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield from iter_bytes(str(p.relative_to(path)), p.read_bytes())
    else:
        yield from iter_bytes(path.name, path.read_bytes())
