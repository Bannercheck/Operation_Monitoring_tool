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


def _parse_one(fname: str, text: str, mapping: dict | None, progress=None, base: float = 0.0, span: float = 1.0) -> tuple[list[Observation], dict]:
    """Parse one file (runs in the main process or in a worker); pure function of its inputs, so the order of results
    is fixed by the caller and the outcome is identical either way."""
    if tables.is_side_table(fname):        # reference data (dependencies, inventory, dictionary): kept, not parsed as events
        rows = tables.read_table(fname, text)
        return [], {"file": fname, "format": "table", "confidence": 1.0, "rows": len(rows), "kind": "table",
                    "keys": list(rows[0].keys()) if rows else [], "roles": {}, "table": rows, "sha": stamp.file_id(fname, text)}
    fmt, conf = detect_format(text)
    parser = PARSERS[fmt]
    probe = text[:400_000] if getattr(parser, "line_oriented", False) else text    # line formats: probe a prefix, never split a 300 MB file twice
    head = [r for _, r in itertools.islice(parser.records(probe), 50)]
    keys: list[str] = []
    for r in head:
        keys.extend(k for k in r if k not in keys)
    learned = None
    if mapping is None and keys and fmt in ("json", "jsonl", "csv", "kv", "grok"):          # a shape seen before: reuse the roles decided then
        from . import profiles as wo_profiles
        learned = wo_profiles.lookup(fmt, keys)
        if learned:
            mapping = dict(learned["mapping"])
    roles = auto_map(keys, head, mapping)
    if progress is None:
        rows = list(parser.parse(text, fname, mapping, conf))
    else:                                                    # report every 5000 rows: share of this file's lines done
        rows = []
        n_lines = max(text.count("\n"), 1)
        progress(base, fname)
        for o in parser.parse(text, fname, mapping, conf):
            rows.append(o)
            if len(rows) % 5000 == 0:
                progress(base + span * min(o.line_no / n_lines, 1.0), fname)
        progress(base + span, fname)
    if keys and fmt in ("json", "jsonl", "csv", "kv", "grok"):
        try:
            from . import profiles as wo_profiles
            wo_profiles.remember(fmt, keys, {k: v for k, v in roles.items() if v}, name=fname, source="user" if (mapping and not learned) else "auto")
        except Exception:  # noqa: BLE001 - remembering is a convenience, never a parse failure
            pass
    return rows, {"file": fname, "format": fmt, "confidence": conf, "rows": len(rows),
                  "kind": rows[0].kind if rows else "-", "keys": keys, "roles": roles, "profile": (learned or {}).get("id", ""), "sha": stamp.file_id(fname, text)}


def _workers(items: list[tuple[str, str]]) -> int:
    import os
    total = sum(len(t) for _, t in items)
    flag = os.environ.get("WATCHOVER_PARALLEL", "")
    cpus = os.cpu_count() or 2
    if flag == "0" or len(items) < PARALLEL_MIN_FILES or total < PARALLEL_MIN_BYTES or (flag != "1" and cpus < PARALLEL_MIN_CPUS):
        return 1
    return max(1, min(len(items), cpus - 2, 8))


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None, progress=None) -> tuple[list[Observation], list[dict]]:
    """progress(fraction 0..1, current file name) is called while parsing (by bytes of input done), so a UI can show a percentage."""
    mapping = mapping or scenario.MAPPING or None
    items = list(files)
    total_bytes = sum(len(t) for _, t in items) or 1
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
                done_bytes = 0
                for i in order:                                                       # completion order ~ submission order
                    futs[i].result(); done_bytes += len(items[i][1])
                    if progress:
                        progress(done_bytes / total_bytes, items[i][0])
                results = [futs[i].result() for i in range(len(items))]             # back in the caller's order
        except Exception:  # noqa: BLE001  (no fork on this platform, pickling limits...): the sequential path is always right
            results = None
    if results is None:
        results, done_bytes = [], 0
        for fname, text in items:
            results.append(_parse_one(fname, text, mapping, progress, done_bytes / total_bytes, len(text) / total_bytes))
            done_bytes += len(text)
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


def ingest_bytes(name: str, data: bytes, mapping: dict | None = None, progress=None):
    return ingest(iter_bytes(name, data), mapping, progress)
