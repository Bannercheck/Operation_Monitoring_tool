"""Ingest: bytes / path -> Observations (+ per-file report). Runs loader -> format detector -> parser."""

from __future__ import annotations

from typing import Iterator

import itertools

from .format_detector import detect_format
from .loader import iter_bytes, iter_path
from .models import Observation
from .normalize import auto_map
from .parsers import PARSERS
from . import scenario


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    mapping = mapping or scenario.MAPPING or None
    observations: list[Observation] = []
    report: list[dict] = []
    for fname, text in files:
        fmt, conf = detect_format(text)
        parser = PARSERS[fmt]
        head = [r for _, r in itertools.islice(parser.records(text), 50)]
        keys: list[str] = []
        for r in head:
            keys.extend(k for k in r if k not in keys)
        roles = auto_map(keys, head, mapping)
        rows = list(parser.parse(text, fname, mapping, conf))
        observations.extend(rows)
        report.append({"file": fname, "format": fmt, "confidence": conf, "rows": len(rows),
                       "kind": rows[0].kind if rows else "-", "keys": keys, "roles": roles})
    fill_missing_timestamps(observations)
    observations.sort(key=lambda o: o.timestamp)
    return observations, report


def fill_missing_timestamps(observations: list[Observation]) -> None:
    """Rows without a parseable time get the earliest real time of the dataset (so charts keep a sane range)."""
    real = [o.timestamp for o in observations if not o.attributes.get("_no_ts")]
    fallback = min(real) if real else observations[0].timestamp if observations else None
    if fallback is None:
        return
    for o in observations:
        if o.attributes.pop("_no_ts", False):
            o.timestamp = fallback


def ingest_path(path: str, mapping: dict | None = None):
    return ingest(iter_path(path), mapping)


def ingest_bytes(name: str, data: bytes, mapping: dict | None = None):
    return ingest(iter_bytes(name, data), mapping)
