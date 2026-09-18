"""Format detector: looks at the first 200 non-empty lines and picks json / jsonl / csv / syslog / kv / text."""

from __future__ import annotations

import json
import re

SYSLOG_RE = re.compile(
    r"^(?:<(?P<pri>\d+)>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+"
    r"(?P<host>\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")
KV_RE = re.compile(r'([\w.\-@]+)=("(?:[^"\\]|\\.)*"|\S+)')
DELIMS = ("\t", ",", ";", "|")
SAMPLE_LINES = 200


def detect_delimiter(lines: list[str]) -> str | None:
    """Quote-aware: field counts come from csv.reader, so commas inside quoted messages do not break the check."""
    import csv
    for d in DELIMS:
        try:
            counts = [len(row) for row in csv.reader(lines[:30], delimiter=d)]
        except csv.Error:
            continue
        if counts and min(counts) >= 2 and len(set(counts)) <= 2:
            return d
    return None


def detect_format(text: str) -> tuple[str, float]:
    """Return (format, confidence in 0..1)."""
    lines = [ln for ln in text.splitlines()[:SAMPLE_LINES] if ln.strip()]
    if not lines:
        return "text", 0.0
    first = lines[0].lstrip()
    if first.startswith("[") or (first.startswith("{") and (len(lines) == 1 or not first.rstrip().endswith("}"))):
        try:
            json.loads(text)
            return "json", 0.95
        except json.JSONDecodeError:
            pass
    ok = 0
    for ln in lines:
        try:
            ok += isinstance(json.loads(ln), dict)
        except json.JSONDecodeError:
            pass
    if ok / len(lines) > 0.8:
        return "jsonl", round(ok / len(lines), 2)
    hits = sum(1 for ln in lines if SYSLOG_RE.match(ln))
    if hits / len(lines) > 0.6:
        return "syslog", round(hits / len(lines), 2)
    from .parsers.sap_parser import sap_score          # SAP families (dev traces, HANA, NW Java, JUL, GC, tp, SM21)
    sap = sap_score(lines)
    if sap >= 0.5:
        return "sap", sap
    d = detect_delimiter(lines)
    if d and len(lines) >= 2:
        import csv as _csv
        header = next(_csv.reader([lines[0]], delimiter=d))
        alpha = sum(1 for h in header if h.strip().replace("_", "").replace(" ", "").isalpha())
        if alpha / len(header) > 0.6:
            return "csv", 0.9
    kv = sum(1 for ln in lines if len(KV_RE.findall(ln)) >= 2)
    if kv / len(lines) > 0.6:
        return "kv", round(kv / len(lines), 2)
    return "text", 0.5
