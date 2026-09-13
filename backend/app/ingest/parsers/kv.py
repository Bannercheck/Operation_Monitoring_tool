"""key=value (logfmt) parser."""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.model import Observation
from .base import Parser

PAIR_RE = re.compile(r'([\w.\-@]+)=("(?:[^"\\]|\\.)*"|\S+)')


class KvParser(Parser):
    name = "kv"
    kind = "log"

    def sniff(self, lines: list[str]) -> float:
        if not lines or lines[0].lstrip().startswith(("{", "[")):
            return 0.0
        return sum(1 for ln in lines if len(PAIR_RE.findall(ln)) >= 2) / len(lines) * 0.9

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        def records():
            for i, ln in enumerate(text.splitlines(), 1):
                pairs = PAIR_RE.findall(ln)
                if len(pairs) < 2:
                    continue
                yield i, {k: v[1:-1].replace('\\"', '"') if v.startswith('"') else v for k, v in pairs}

        yield from self.records_to_observations(records(), source, mapping)
