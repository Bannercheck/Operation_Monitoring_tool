"""Parser interface plus a shared record -> Observation builder."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from itertools import chain, islice
from typing import Any

from app.model import Observation
from app.ingest.common import (infer_severity_from_text, normalize_severity, parse_timestamp,
                               resolve_mapping)


class Parser:
    name = "base"
    kind = "log"

    def sniff(self, lines: list[str]) -> float:
        """Confidence in [0, 1] that this parser fits the sample lines."""
        raise NotImplementedError

    def parse(self, text: str, source: str, mapping: dict | None = None) -> Iterator[Observation]:
        raise NotImplementedError

    def records_to_observations(self, records: Iterator[tuple[int, dict[str, Any]]], source: str,
                                mapping: dict | None) -> Iterator[Observation]:
        """Turn (line_no, flat dict) records into Observations using a resolved role mapping."""
        records = iter(records)
        head = list(islice(records, 50))  # resolve roles from the union of keys in the first records
        keys: list[str] = []
        for _, rec in head:
            keys.extend(k for k in rec if k not in keys)
        roles = resolve_mapping(keys, mapping)
        last_ts: datetime | None = None
        for line_no, rec in chain(head, records):
            ts = parse_timestamp(rec.get(roles["timestamp"])) if roles["timestamp"] else None
            if ts is None:
                ts = last_ts or datetime.fromtimestamp(0, tz=timezone.utc)
            last_ts = ts
            msg = str(rec.get(roles["message"], "")) if roles["message"] else " ".join(f"{k}={v}" for k, v in rec.items())
            sev_raw = rec.get(roles["severity"]) if roles["severity"] else None
            sev = normalize_severity(sev_raw) if sev_raw not in (None, "") else (infer_severity_from_text(msg) or "INFO")
            used = {v for v in roles.values() if v}
            yield Observation(
                timestamp=ts, message=msg, kind=self.kind, severity=sev,
                service=str(rec.get(roles["service"], "") or "") if roles["service"] else "",
                host=str(rec.get(roles["host"], "") or "") if roles["host"] else "",
                attributes={k: v for k, v in rec.items() if k not in used and v not in (None, "")},
                source=source, line_no=line_no, parser=self.name,
            )
