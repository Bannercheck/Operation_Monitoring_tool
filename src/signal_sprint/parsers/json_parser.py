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
        if isinstance(obj, dict):
            obj = next((v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [obj])
        for i, r in enumerate(obj, 1):
            if isinstance(r, dict):
                yield i, flatten(r)
