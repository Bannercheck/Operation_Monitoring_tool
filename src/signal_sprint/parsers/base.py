"""Parser interface: text -> (line_no, record dict) rows. common.records_to_observations turns rows into Observations."""

from __future__ import annotations

from typing import Iterator

from ..models import Observation
from .common import records_to_observations


class Parser:
    name = "base"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        raise NotImplementedError

    def parse(self, text: str, source: str, mapping: dict | None = None, confidence: float = 1.0) -> Iterator[Observation]:
        yield from records_to_observations(self.records(text), source, self.name, self.kind, mapping, confidence)
