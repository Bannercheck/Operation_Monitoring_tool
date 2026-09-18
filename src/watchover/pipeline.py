"""Ingest: bytes / path -> Observations (+ per-file report). Runs loader -> format detector -> parser."""

from __future__ import annotations

from typing import Iterator

import itertools

from .format_detector import detect_format
from .loader import iter_bytes, iter_path
from .models import Observation
from .normalize import auto_map
from .parsers import PARSERS
from . import scenario, tables


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    mapping = mapping or scenario.MAPPING or None
    observations: list[Observation] = []
    report: list[dict] = []
    for fname, text in files:
        if tables.is_side_table(fname):        # reference data (dependencies, inventory, dictionary): kept, not parsed as events
            rows = tables.read_table(fname, text)
            report.append({"file": fname, "format": "table", "confidence": 1.0, "rows": len(rows), "kind": "table",
                           "keys": list(rows[0].keys()) if rows else [], "roles": {}, "table": rows})
            continue
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
    observations = dedupe(observations, report)
    fill_missing_timestamps(observations)
    observations.sort(key=lambda o: o.timestamp)
    return observations, report


def dedupe(observations: list[Observation], report: list[dict]) -> list[Observation]:
    """The same events shipped in two formats (alarms.json + alarms.csv): keep one per DEDUP_KEY, prefer DEDUP_PREFER."""
    key = scenario.DEDUP_KEY
    if not key:
        return observations
    keep: dict[str, Observation] = {}
    order: list[str] = []
    dropped: dict[str, int] = {}
    passthrough: list[Observation] = []
    for o in observations:
        k = o.attributes.get(key)
        if k in (None, ""):
            passthrough.append(o)
            continue
        k = str(k)
        if k not in keep:
            keep[k] = o
            order.append(k)
        else:
            cur = keep[k]
            if o.parser == scenario.DEDUP_PREFER and cur.parser != scenario.DEDUP_PREFER:
                dropped[cur.source] = dropped.get(cur.source, 0) + 1
                keep[k] = o
            else:
                dropped[o.source] = dropped.get(o.source, 0) + 1
    if not dropped:
        return observations
    for entry in report:
        if entry["file"] in dropped:
            entry["dedup_dropped"] = dropped[entry["file"]]
    return passthrough + [keep[k] for k in order]


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
