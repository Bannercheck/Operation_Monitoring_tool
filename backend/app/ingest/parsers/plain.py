"""Plain text fallback: leading timestamp + optional level + rest of line."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone

from app.model import Observation
from app.ingest.common import infer_severity_from_text, parse_timestamp
from .base import Parser

TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+\-]\d{2}:?\d{2})?)\]?\s*")
LEVEL_RE = re.compile(r"^\[?(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL)\]?[:\s]+", re.I)
LOGGER_RE = re.compile(r"^\[?([\w.\-]+)\]?[:\s]+")


class PlainParser(Parser):
    name = "plain"
    kind = "log"

    def sniff(self, lines: list[str]) -> float:
        return 0.1 if lines else 0.0

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        last_ts = datetime.fromtimestamp(0, tz=timezone.utc)
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            rest, ts = ln, None
            m = TS_RE.match(rest)
            if m:
                ts = parse_timestamp(m[1])
                rest = rest[m.end():]
            ts = ts or last_ts
            last_ts = ts
            sev = "INFO"
            m = LEVEL_RE.match(rest)
            if m:
                sev = infer_severity_from_text(m[1]) or "INFO"
                rest = rest[m.end():]
            service = ""
            m = LOGGER_RE.match(rest)
            if m and "." in m[1] or (m and m[1].islower() and len(m[1]) > 2 and not rest[m.end():].startswith("=")):
                service, rest = m[1], rest[m.end():]
            yield Observation(timestamp=ts, message=rest.strip(), kind=self.kind, severity=sev,
                              service=service, source=source, line_no=i, parser=self.name)
