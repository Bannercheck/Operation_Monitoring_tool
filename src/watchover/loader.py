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
MAX_TOTAL_BYTES = 1 << 30          # 1 GiB of decompressed content per upload (decompression-bomb ceiling)
MAX_MEMBER_BYTES = 512 << 20       # 512 MiB for any single file inside an archive
MAX_MEMBERS = 20000                # at most this many files across all nested archives


class ArchiveTooBig(ValueError):
    """An upload expanded past the decompression limits (bomb protection)."""


class _Budget:
    __slots__ = ("total", "members")

    def __init__(self):
        self.total = 0
        self.members = 0

    def take(self, n: int) -> None:
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise ArchiveTooBig(f"too many files in archive (> {MAX_MEMBERS})")
        if n > MAX_MEMBER_BYTES:
            raise ArchiveTooBig(f"a file exceeds {MAX_MEMBER_BYTES} bytes")
        self.total += n
        if self.total > MAX_TOTAL_BYTES:
            raise ArchiveTooBig(f"archive expands past {MAX_TOTAL_BYTES} bytes")


def decode(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return None
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def iter_bytes(name: str, data: bytes, _budget: "_Budget | None" = None) -> Iterator[tuple[str, str]]:
    budget = _budget or _Budget()
    lower = name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX"):
                    continue
                if info.file_size > MAX_MEMBER_BYTES:                 # trust the header first, so a bomb is refused before read()
                    raise ArchiveTooBig(f"{info.filename}: declared size {info.file_size} exceeds limit")
                yield from iter_bytes(info.filename, zf.read(info), budget)
        return
    if lower.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                if m.size > MAX_MEMBER_BYTES:
                    raise ArchiveTooBig(f"{m.name}: declared size {m.size} exceeds limit")
                f = tf.extractfile(m)
                if f is not None:
                    yield from iter_bytes(m.name, f.read(), budget)
        return
    if lower.endswith(".gz"):
        raw = gzip.decompress(data)
        budget.take(len(raw))
        yield from iter_bytes(name[:-3], raw, budget)
        return
    if Path(lower).suffix in BINARY_EXT:
        return
    budget.take(len(data))
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
