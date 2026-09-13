"""key=value (logfmt) lines."""

from __future__ import annotations

from typing import Iterator

from ..format_detector import KV_RE
from .base import Parser


class KvParser(Parser):
    name = "kv"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            pairs = KV_RE.findall(ln)
            if len(pairs) >= 2:
                yield i, {k: v[1:-1].replace('\\"', '"') if v.startswith('"') else v for k, v in pairs}
