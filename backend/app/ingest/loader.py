"""Universal loader: accepts a file, ZIP or GZ and yields (name, text) pairs."""

from __future__ import annotations

import gzip
import io
import zipfile
from collections.abc import Iterator


def iter_files(name: str, data: bytes) -> Iterator[tuple[str, str]]:
    """Yield (file_name, text) for every text file inside the upload."""
    lower = name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                yield from iter_files(info.filename, zf.read(info))
        return
    if lower.endswith(".gz"):
        yield from iter_files(name[:-3], gzip.decompress(data))
        return
    yield name, data.decode("utf-8", errors="replace")
