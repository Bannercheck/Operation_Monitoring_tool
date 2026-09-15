"""Shared record -> Observation conversion using the schema auto-mapper."""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from typing import Iterator

from ..models import Observation
from ..normalize import UTC, auto_map, infer_environment, normalize_environment, normalize_severity, parse_timestamp, severity_from_text

HEAD = 50  # records used to resolve the schema
MISSING_TS = datetime.fromtimestamp(0, tz=UTC)  # placeholder, replaced in pipeline.ingest


def records_to_observations(records: Iterator[tuple[int, dict]], source: str, parser: str, kind: str,
                            mapping: dict | None = None, confidence: float = 1.0, lines: list[str] | None = None) -> Iterator[Observation]:
    records = iter(records)
    head = list(itertools.islice(records, HEAD))
    keys: list[str] = []
    for _, rec in head:
        keys.extend(k for k in rec if k not in keys)
    roles = auto_map(keys, [r for _, r in head], mapping)
    used = {v for v in roles.values() if v}
    if "alert" in source.lower() or roles["message"] in ("alert", "alertname"):
        kind = "alert"
    elif "incident" in source.lower():
        kind = "incident"
    last_ts: datetime | None = None
    for line_no, rec in itertools.chain(head, records):
        ts = parse_timestamp(rec.get(roles["timestamp"])) if roles["timestamp"] else None
        missing = ts is None
        ts = ts or last_ts or MISSING_TS
        if not missing:
            last_ts = ts
        msg = str(rec.get(roles["message"]) or "") if roles["message"] else " ".join(f"{k}={v}" for k, v in rec.items())
        sev_raw = rec.get(roles["severity"]) if roles["severity"] else None
        sev = normalize_severity(sev_raw) if sev_raw not in (None, "") else severity_from_text(msg)
        service = str(rec.get(roles["service"]) or "") if roles["service"] else ""
        host = str(rec.get(roles["host"]) or "") if roles["host"] else ""
        env = normalize_environment(rec.get(roles["environment"])) if roles.get("environment") else ""
        yield Observation(
            timestamp=ts, message=msg, kind=kind, severity=sev, service=service, host=host,
            environment=env or infer_environment(host, service, source, msg),
            origin=str(rec.get(roles["origin"]) or "") if roles.get("origin") else "",
            attributes={**{k: v for k, v in rec.items() if k not in used and v not in (None, "")}, **({"_no_ts": True} if missing else {})},
            source=source, line_no=line_no, parser=parser, parser_confidence=confidence,
            raw=(lines[line_no - 1] if lines and 0 < line_no <= len(lines) else json.dumps(rec, ensure_ascii=False, default=str)))
