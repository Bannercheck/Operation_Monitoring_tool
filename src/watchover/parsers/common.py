"""Shared record -> Observation conversion using the schema auto-mapper."""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from typing import Iterator

from ..models import Observation
from ..normalize import UTC, auto_map, column_severity, infer_environment, normalize_environment, parse_timestamp, severity_from_text

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
    r_ts, r_msg, r_sev, r_svc, r_host = roles["timestamp"], roles["message"], roles["severity"], roles["service"], roles["host"]
    r_env, r_origin = roles.get("environment"), roles.get("origin")
    use_scale = parser != "syslog"
    env_of: dict[tuple, str] = {}                       # (host, service) -> environment from names; the message is only read when they say nothing
    n_lines = len(lines) if lines else 0
    for line_no, rec in itertools.chain(head, records):
        ts = parse_timestamp(rec.get(r_ts)) if r_ts else None
        missing = ts is None
        ts = ts or last_ts or MISSING_TS
        if not missing:
            last_ts = ts
        msg = str(rec.get(r_msg) or "") if r_msg else " ".join(f"{k}={v}" for k, v in rec.items())
        sev_raw = rec.get(r_sev) if r_sev else None
        sev = column_severity(sev_raw, use_scale=use_scale) if sev_raw not in (None, "") else severity_from_text(msg)
        service = str(rec.get(r_svc) or "") if r_svc else ""
        host = str(rec.get(r_host) or "") if r_host else ""
        env = normalize_environment(rec.get(r_env)) if r_env else ""
        if not env:
            pair = (host, service)
            env = env_of.get(pair)
            if env is None:
                env = env_of[pair] = infer_environment(host, service, source)
            if not env:
                env = infer_environment(msg)
        attrs = {k: v for k, v in rec.items() if k not in used and v not in (None, "")} if len(rec) > len(used) else {}
        if sev_raw is not None and str(sev_raw).strip().isdigit():
            attrs["severity_raw"] = sev_raw
        if missing:
            attrs["_no_ts"] = True
        yield Observation(
            timestamp=ts, message=msg, kind=kind, severity=sev, service=service, host=host, environment=env,
            origin=str(rec.get(r_origin) or "") if r_origin else "", attributes=attrs,
            source=source, line_no=line_no, parser=parser, parser_confidence=confidence,
            raw=(lines[line_no - 1] if 0 < line_no <= n_lines else json.dumps(rec, ensure_ascii=False, default=str)))
