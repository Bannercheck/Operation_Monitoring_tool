"""CSV / TSV with delimiter detection (',', ';', '|', tab)."""

from __future__ import annotations

import csv
import io
from typing import Iterator

from ..format_detector import detect_delimiter
from .base import Parser


class CsvParser(Parser):
    name = "csv"
    kind = "event"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        d = detect_delimiter(lines) or ","
        for i, row in enumerate(csv.DictReader(io.StringIO(text), delimiter=d), 2):  # header is line 1
            yield i, {k.strip(): v for k, v in row.items() if k is not None}
