"""JSONL / NDJSON: one JSON object per line."""

from __future__ import annotations

import json
from typing import Iterator

from ..normalize import flatten
from .base import Parser


class JsonlParser(Parser):
    name = "jsonl"
    kind = "event"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                yield i, flatten(r)
