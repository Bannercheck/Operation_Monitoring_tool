"""CSV/TSV parser with delimiter detection."""

from __future__ import annotations

import csv as _csv
import io
from collections.abc import Iterator

from app.model import Observation
from .base import Parser

DELIMS = ("\t", ",", ";", "|")


def detect_delimiter(lines: list[str]) -> str | None:
    for d in DELIMS:
        counts = [ln.count(d) for ln in lines[:30]]
        if counts and min(counts) >= 1 and len(set(counts)) <= 2:
            return d
    return None


class CsvParser(Parser):
    name = "csv"
    kind = "event"

    def sniff(self, lines: list[str]) -> float:
        if len(lines) < 2 or lines[0].lstrip().startswith(("{", "[")):
            return 0.0
        d = detect_delimiter(lines)
        if d is None:
            return 0.0
        header = lines[0].split(d)
        alpha = sum(1 for h in header if h.strip().replace("_", "").replace(" ", "").isalpha())
        return 0.9 if alpha / len(header) > 0.6 else 0.5

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        d = detect_delimiter(lines) or ","
        reader = _csv.DictReader(io.StringIO(text), delimiter=d)

        def records():
            for i, row in enumerate(reader, 2):  # header is line 1
                yield i, {k.strip(): v for k, v in row.items() if k is not None}

        yield from self.records_to_observations(records(), source, mapping)
