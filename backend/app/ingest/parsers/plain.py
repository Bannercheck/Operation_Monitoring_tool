"""plain parser (TODO)."""

from __future__ import annotations

from collections.abc import Iterator

from app.model import Observation
from .base import Parser


class PlainParser(Parser):
    name = "plain"

    def sniff(self, lines: list[str]) -> float:
        return 0.0

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        yield from ()
