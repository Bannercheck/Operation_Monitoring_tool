"""JSON array, or an object wrapping an array of records."""

from __future__ import annotations

import json
from typing import Iterator

from ..normalize import flatten
from .base import Parser


class JsonParser(Parser):
    name = "json"
    kind = "event"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        obj = json.loads(text)
        rows = find_records(obj) or ([obj] if isinstance(obj, dict) else [])
        for i, r in enumerate(rows, 1):
            if isinstance(r, dict):
                yield i, flatten(r)


def find_records(obj, depth: int = 0) -> list | None:
    """Depth-first: the first list of dicts anywhere in the document (e.g. {"status":..,"data":{"alerts":[...]}})."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else None
    if isinstance(obj, dict) and depth < 4:
        for v in obj.values():
            found = find_records(v, depth + 1)
            if found:
                return found
    return None
