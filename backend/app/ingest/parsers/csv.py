"""csv parser (TODO)."""

from __future__ import annotations

from collections.abc import Iterator

from app.model import Observation
from .base import Parser


class CsvParser(Parser):
    name = "csv"

    def sniff(self, lines: list[str]) -> float:
        return 0.0

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        yield from ()
