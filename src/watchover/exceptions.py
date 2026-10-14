"""Exceptions: error lines grouped by what failed (exception class, ORA / SQL code, HTTP 5xx, timeout, refused connection) and the
service that logged it, with hosts, first / last seen, a per-minute trend and one masked sample. Deterministic, no stack-trace parser needed."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

UTC = timezone.utc

from .privacy import scrub

TYPES = [
    (re.compile(r"\b((?:[a-z_][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error|Fault|Panic))\b"), None),
    (re.compile(r"\b(ORA-\d{5}|SQLSTATE\[?\w{5}\]?|SQL\d{4,5}N?|MSG \d{3,5}|ERROR \d{4}|ECONN\w+|ETIMEDOUT|ENOTFOUND)\b"), None),
    (re.compile(r"\b(?:status|HTTP)[ =:/]*(5\d\d)\b", re.I), "HTTP {0}"),
    (re.compile(r"\b(timed? ?out|zaman aşımı|timeout)\b", re.I), "Timeout"),
    (re.compile(r"\b(connection refused|bağlantı reddedildi|connection reset)\b", re.I), "ConnectionRefused"),
    (re.compile(r"\b(out of memory|OOM|heap space)\b", re.I), "OutOfMemory"),
    (re.compile(r"\b(deadlock)\b", re.I), "Deadlock"),
    (re.compile(r"\b(permission denied|access denied|yetkisiz|unauthori[sz]ed|forbidden)\b", re.I), "AccessDenied"),
]


def exception_type(message: str) -> str | None:
    for rx, fmt in TYPES:
        m = rx.search(message or "")
        if m:
            return fmt.format(m[1]) if fmt else m[1]
    return None


def group(observations, minutes: int = 60, now: datetime | None = None, top: int = 200) -> list[dict]:
    now = now or datetime.now(UTC)
    g: dict[tuple, dict] = {}
    for o in observations:
        if o.severity not in ("ERROR", "CRITICAL"):
            continue
        ts = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)
        age = (now - ts).total_seconds() / 60
        if age > minutes or age < -5:
            continue
        typ = exception_type(o.message) or "Error"
        k = (typ, o.service or "-")
        e = g.get(k)
        if e is None:
            e = g[k] = {"type": typ, "service": o.service or "-", "count": 0, "hosts": defaultdict(int), "first": ts, "last": ts,
                        "sample": scrub(o.message or "")[:400], "ref": o.ref, "trend": [0] * min(60, minutes), "critical": 0}
        e["count"] += 1
        e["critical"] += o.severity == "CRITICAL"
        e["hosts"][o.host or "-"] += 1
        if ts < e["first"]:
            e["first"] = ts
        if ts >= e["last"]:
            e["last"], e["sample"], e["ref"] = ts, scrub(o.message or "")[:400], o.ref
        b = int(age * len(e["trend"]) / minutes) if minutes else 0
        if 0 <= b < len(e["trend"]):
            e["trend"][len(e["trend"]) - 1 - b] += 1
    out = []
    for e in g.values():
        hosts = sorted(e["hosts"].items(), key=lambda x: -x[1])
        out.append({**e, "hosts": [{"host": h, "count": c} for h, c in hosts[:20]], "host_count": len(hosts),
                    "first": e["first"].isoformat(timespec="seconds"), "last": e["last"].isoformat(timespec="seconds")})
    return sorted(out, key=lambda x: (-x["count"], x["type"]))[:top]
