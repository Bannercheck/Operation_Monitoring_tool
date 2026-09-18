"""key=value (logfmt) lines."""

from __future__ import annotations

from typing import Iterator

from ..format_detector import KV_RE
from .base import Parser
from .text_parser import LEVEL_RE, LOGGER_RE, TS_RE


class KvParser(Parser):
    name = "kv"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            rec: dict = {}
            rest = ln
            if m := TS_RE.match(rest):
                rec["timestamp"], rest = m[1], rest[m.end():]
            if m := LEVEL_RE.match(rest):
                rec["severity"], rest = m[1], rest[m.end():]
            if (m := LOGGER_RE.match(rest)) and "=" not in m[1]:
                rec["service"], rest = m[1], rest[m.end():]
            pairs = KV_RE.findall(rest)
            if len(pairs) >= 2 or (pairs and rec):
                rec.update({k: v[1:-1].replace('\\"', '"') if v.startswith('"') else v for k, v in pairs})
                yield i, rec
