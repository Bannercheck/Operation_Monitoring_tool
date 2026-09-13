"""Ingest entry point: upload bytes -> list[Observation]."""

from __future__ import annotations

from app.model import Observation
from .detect import detect_format
from .loader import iter_files
from .parsers import PARSERS


def ingest(name: str, data: bytes, mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    """Return observations plus a per-file report (format, confidence, rows)."""
    observations: list[Observation] = []
    report: list[dict] = []
    for fname, text in iter_files(name, data):
        fmt, conf = detect_format(text)
        rows = list(PARSERS[fmt].parse(text, fname, mapping))
        for o in rows:
            o.parser, o.parser_confidence = fmt, conf
        observations.extend(rows)
        report.append({"file": fname, "format": fmt, "confidence": conf, "rows": len(rows)})
    observations.sort(key=lambda o: o.timestamp)
    return observations, report
