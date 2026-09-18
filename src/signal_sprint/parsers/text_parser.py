"""Generic text fallback: leading timestamp, optional level, optional logger name, rest is the message."""

from __future__ import annotations

import re
from typing import Iterator

from .base import Parser

TS_RE = re.compile(r"^\[?("
                   r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+\-]\d{2}:?\d{2})?"          # ISO
                   r"|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"                                       # 2026/09/16 02:14:07
                   r"|\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}"                                               # 16.09.2026 02:14:07
                   r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}"          # Wed Sep 16 02:14:07 2026
                   r"|[A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)"                           # Sep 16, 2026 2:14:07 AM
                   r")\]?:?\s*")
LEVEL_RE = re.compile(r"^\[?(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|SEVERE|CONFIG|FINE(?:R|ST)?)\]?[:\s]+", re.I)
LOGGER_RE = re.compile(r"^\[?([A-Za-z][\w.\-]{2,})\]?:\s+")


class TextParser(Parser):
    name = "text"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            rec: dict = {}
            rest = ln
            if m := TS_RE.match(rest):
                rec["timestamp"], rest = m[1], rest[m.end():]
            if m := LEVEL_RE.match(rest):
                rec["severity"], rest = m[1], rest[m.end():]
            if m := LOGGER_RE.match(rest):
                rec["service"], rest = m[1], rest[m.end():]
            rec["message"] = rest.strip()
            yield i, rec
