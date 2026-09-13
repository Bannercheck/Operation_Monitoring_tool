"""Syslog-like lines: 'Sep 16 14:31:02 host program[pid]: message' with optional <PRI>."""

from __future__ import annotations

from typing import Iterator

from ..format_detector import SYSLOG_RE
from .base import Parser


class SyslogParser(Parser):
    name = "syslog"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            m = SYSLOG_RE.match(ln)
            if not m:
                continue
            rec = {"timestamp": m["ts"], "host": m["host"], "program": m["prog"], "message": m["msg"]}
            if m["pid"]:
                rec["pid"] = m["pid"]
            if m["pri"]:
                rec["severity"] = str(int(m["pri"]) % 8)
            yield i, rec
