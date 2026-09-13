"""Ingest: bytes / path -> Observations (+ per-file report). Runs loader -> format detector -> parser."""

from __future__ import annotations

from typing import Iterator

from .format_detector import detect_format
from .loader import iter_bytes, iter_path
from .models import Observation
from .parsers import PARSERS
from . import scenario


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    mapping = mapping or scenario.MAPPING or None
    observations: list[Observation] = []
    report: list[dict] = []
    for fname, text in files:
        fmt, conf = detect_format(text)
        rows = list(PARSERS[fmt].parse(text, fname, mapping, conf))
        observations.extend(rows)
        report.append({"file": fname, "format": fmt, "confidence": conf, "rows": len(rows),
                       "kind": rows[0].kind if rows else "-"})
    observations.sort(key=lambda o: o.timestamp)
    return observations, report


def ingest_path(path: str, mapping: dict | None = None):
    return ingest(iter_path(path), mapping)


def ingest_bytes(name: str, data: bytes, mapping: dict | None = None):
    return ingest(iter_bytes(name, data), mapping)
