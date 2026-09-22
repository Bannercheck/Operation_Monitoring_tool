"""JSON array, or an object wrapping an array of records."""

from __future__ import annotations

import json
from typing import Iterator

from ..normalize import flatten
from .base import Parser


class JsonParser(Parser):
    name = "json"
    kind = "event"
    line_oriented = False

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        obj = json.loads(text)
        if isinstance(obj, dict) and "resourceLogs" in obj:
            rows = otel_records(obj)
        else:
            rows = find_records(obj) or ([obj] if isinstance(obj, dict) else [])
        for i, r in enumerate(rows, 1):
            if isinstance(r, dict):
                yield i, enrich(flatten(r))


def _otel_attrs(items) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value", {})
        out[a.get("key", "")] = next(iter(v.values()), None) if isinstance(v, dict) else v
    return out


def otel_records(obj: dict) -> list[dict]:
    """OpenTelemetry OTLP/JSON logs: resource attributes (service.name, host.name) + each logRecord flattened."""
    rows = []
    for rl in obj.get("resourceLogs", []):
        res = _otel_attrs(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []) or rl.get("instrumentationLibraryLogs", []):
            for lr in sl.get("logRecords", []):
                body = lr.get("body", {})
                rows.append({**res, "timestamp": lr.get("timeUnixNano") or lr.get("observedTimeUnixNano"), "severity": lr.get("severityText") or lr.get("severityNumber"),
                             "message": next(iter(body.values()), "") if isinstance(body, dict) else body, **_otel_attrs(lr.get("attributes"))})
    return rows


def enrich(rec: dict) -> dict:
    """Records whose text field carries its own '<ts> LEVEL app message' (docker json-file, CRI, GELF full_message): lift level and app."""
    from ..normalize import ROLE_HINTS
    from .text_parser import parse_head
    if any(k in rec for k in ("severity", "level", "log.level", "severityText", "PRIORITY", "s")):
        return rec
    txt = next((rec[k] for k in ("log", "message", "msg", "short_message") if isinstance(rec.get(k), str)), None)
    if not txt:
        return rec
    head, rest = parse_head(txt.rstrip("\n"))
    if head.get("severity"):
        rec["severity"] = head["severity"]
        if head.get("service") and not any(k in rec for k in ROLE_HINTS["service"]):
            rec["service"] = head["service"]
        if rest:
            rec["message"] = rest
    return rec


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
