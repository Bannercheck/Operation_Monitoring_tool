"""JSON parser: one object per line (JSONL), a JSON array, or an object wrapping an array."""

from __future__ import annotations

import json
from collections.abc import Iterator

from app.model import Observation
from app.ingest.common import flatten
from .base import Parser


class JsonlParser(Parser):
    name = "jsonl"
    kind = "event"

    def sniff(self, lines: list[str]) -> float:
        if not lines:
            return 0.0
        first = lines[0].lstrip()
        if first.startswith("[") or (first.startswith("{") and not first.rstrip().endswith("}")):
            try:
                json.loads("\n".join(lines))
                return 0.95
            except json.JSONDecodeError:
                return 0.6 if first.startswith("[") else 0.0
        ok = 0
        for ln in lines:
            try:
                ok += isinstance(json.loads(ln), dict)
            except json.JSONDecodeError:
                pass
        return ok / len(lines)

    def _records(self, text: str) -> Iterator[tuple[int, dict]]:
        stripped = text.lstrip()
        if stripped.startswith("["):
            for i, r in enumerate(json.loads(text), 1):
                if isinstance(r, dict):
                    yield i, flatten(r)
            return
        lines = text.splitlines()
        if stripped.startswith("{"):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    lst = next((v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [obj])
                    for i, r in enumerate(lst, 1):
                        yield i, flatten(r)
                    return
            except json.JSONDecodeError:
                pass
        for i, ln in enumerate(lines, 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                yield i, flatten(r)

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        yield from self.records_to_observations(self._records(text), source, mapping)
