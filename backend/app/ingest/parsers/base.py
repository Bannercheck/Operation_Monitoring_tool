"""Parser interface. Each parser turns raw text into Observations."""

from __future__ import annotations

from collections.abc import Iterator

from app.model import Observation


class Parser:
    name = "base"

    def sniff(self, lines: list[str]) -> float:
        """Confidence in [0, 1] that this parser fits the sample lines."""
        raise NotImplementedError

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        raise NotImplementedError
