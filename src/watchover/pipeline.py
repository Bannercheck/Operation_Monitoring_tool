"""Ingest: bytes / path -> Observations (+ per-file report). Runs loader -> format detector -> parser."""

from __future__ import annotations

from typing import Iterator

import itertools

from .format_detector import detect_format
from . import stamp
from .loader import iter_bytes, iter_path
from .models import Observation
from .normalize import auto_map
from .parsers import PARSERS
from . import scenario, tables


PARALLEL_MIN_BYTES = 32 * 1024 * 1024    # archives / multi-file uploads above this parse their files on several cores...
PARALLEL_MIN_FILES = 3
PARALLEL_MIN_CPUS = 8                     # ...on machines with enough cores: returning parsed events from a worker costs about
                                          # as much as parsing them (measured: 0.58 s parse vs 0.8 s transfer per 120k lines), so
                                          # fewer cores lose. WATCHOVER_PARALLEL=1 forces it on, =0 off.


def _parse_one(fname: str, text: str, mapping: dict | None) -> tuple[list[Observation], dict]:
    """Parse one file (runs in the main process or in a worker); pure function of its inputs, so the order of results
    is fixed by the caller and the outcome is identical either way."""
    if tables.is_side_table(fname):        # reference data (dependencies, inventory, dictionary): kept, not parsed as events
        rows = tables.read_table(fname, text)
        return [], {"file": fname, "format": "table", "confidence": 1.0, "rows": len(rows), "kind": "table",
                    "keys": list(rows[0].keys()) if rows else [], "roles": {}, "table": rows, "sha": stamp.file_id(fname, text)}
    fmt, conf = detect_format(text)
    parser = PARSERS[fmt]
    head = [r for _, r in itertools.islice(parser.records(text), 50)]
    keys: list[str] = []
    for r in head:
        keys.extend(k for k in r if k not in keys)
    roles = auto_map(keys, head, mapping)
    rows = list(parser.parse(text, fname, mapping, conf))
    return rows, {"file": fname, "format": fmt, "confidence": conf, "rows": len(rows),
                  "kind": rows[0].kind if rows else "-", "keys": keys, "roles": roles, "sha": stamp.file_id(fname, text)}


def _workers(items: list[tuple[str, str]]) -> int:
    import os
    total = sum(len(t) for _, t in items)
    flag = os.environ.get("WATCHOVER_PARALLEL", "")
    cpus = os.cpu_count() or 2
    if flag == "0" or len(items) < PARALLEL_MIN_FILES or total < PARALLEL_MIN_BYTES or (flag != "1" and cpus < PARALLEL_MIN_CPUS):
        return 1
    return max(1, min(len(items), cpus - 2, 8))


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    mapping = mapping or scenario.MAPPING or None
    items = list(files)
    observations: list[Observation] = []
    report: list[dict] = []
    n = _workers(items)
    results = None
    if n > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            order = sorted(range(len(items)), key=lambda i: -len(items[i][1]))      # biggest files first: better packing
            with ProcessPoolExecutor(max_workers=n) as ex:
                futs = {i: ex.submit(_parse_one, items[i][0], items[i][1], mapping) for i in order}
                results = [futs[i].result() for i in range(len(items))]             # back in the caller's order
        except Exception:  # noqa: BLE001  (no fork on this platform, pickling limits...): the sequential path is always right
            results = None
    if results is None:
        results = [_parse_one(fname, text, mapping) for fname, text in items]
    for rows, rep in results:
        observations.extend(rows)
        report.append(rep)
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
