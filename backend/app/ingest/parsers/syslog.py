"""Syslog (RFC3164-ish) parser: 'Sep 16 14:31:02 host program[pid]: message'."""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.model import Observation
from .base import Parser

LINE_RE = re.compile(
    r"^(?:<(?P<pri>\d+)>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+"
    r"(?P<host>\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")


class SyslogParser(Parser):
    name = "syslog"
    kind = "log"

    def sniff(self, lines: list[str]) -> float:
        if not lines:
            return 0.0
        return sum(1 for ln in lines if LINE_RE.match(ln)) / len(lines)

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        def records():
            for i, ln in enumerate(text.splitlines(), 1):
                m = LINE_RE.match(ln)
                if not m:
                    continue
                rec = {"timestamp": m["ts"], "host": m["host"], "program": m["prog"], "message": m["msg"]}
                if m["pid"]:
                    rec["pid"] = m["pid"]
                if m["pri"]:
                    rec["severity"] = str(int(m["pri"]) % 8)
                yield i, rec

        yield from self.records_to_observations(records(), source, mapping)
