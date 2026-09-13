"""Shared helpers for parsers: timestamp parsing, severity normalisation, field picking."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

SEVERITY_MAP = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "informational": "INFO", "notice": "INFO",
    "warn": "WARN", "warning": "WARN", "error": "ERROR", "err": "ERROR", "critical": "CRITICAL",
    "crit": "CRITICAL", "fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL",
    "high": "ERROR", "medium": "WARN", "low": "INFO", "p1": "CRITICAL", "p2": "ERROR", "p3": "WARN", "p4": "INFO",
}

ROLE_HINTS = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created_at", "logged_at", "t"),
    "severity": ("severity", "level", "loglevel", "log_level", "priority", "sev", "status"),
    "service": ("service", "app", "application", "component", "logger", "source", "module", "job", "program", "svc", "service_name"),
    "host": ("host", "hostname", "node", "instance", "server", "pod", "container", "machine"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title", "body", "line", "event"),
}

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%d/%b/%Y:%H:%M:%S %z",  # apache/nginx
)
_SYSLOG_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s(\d{2}):(\d{2}):(\d{2})")


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def parse_timestamp(value: Any, default_year: int | None = None) -> datetime | None:
    """Parse many timestamp shapes; always returns a tz-aware datetime (naive input assumed UTC)."""
    return _aware(_parse_timestamp(value, default_year))


def _parse_timestamp(value: Any, default_year: int | None = None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    s2 = s[:-1] + "+0000" if s.endswith("Z") else s
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    m = _SYSLOG_RE.match(s)
    if m:
        year = default_year or datetime.now().year
        try:
            return datetime.strptime(f"{year} {m[1]} {m[2]} {m[3]}:{m[4]}:{m[5]}", "%Y %b %d %H:%M:%S")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


def normalize_severity(value: Any) -> str:
    if value is None:
        return "INFO"
    s = str(value).strip().lower()
    if s in SEVERITY_MAP:
        return SEVERITY_MAP[s]
    if s.isdigit():  # syslog numeric severity 0..7
        n = int(s)
        return {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR", 4: "WARN"}.get(n, "INFO")
    up = s.upper()
    return up if up in {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"} else "INFO"


def resolve_mapping(keys: list[str], mapping: dict | None = None) -> dict[str, str | None]:
    """Decide which key plays which role. Explicit mapping wins over heuristics."""
    lowered = {k.lower(): k for k in keys}
    out: dict[str, str | None] = {}
    for role, hints in ROLE_HINTS.items():
        if mapping and mapping.get(role) in keys:
            out[role] = mapping[role]
            continue
        hit = next((lowered[h] for h in hints if h in lowered), None)
        if hit is None:
            hit = next((k for lk, k in lowered.items() if any(lk.endswith(sep + h) for h in hints for sep in ("_", ".", "-"))), None)
        out[role] = hit
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def infer_severity_from_text(text: str) -> str | None:
    m = re.search(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|ALERT|EMERG)\b", text, re.I)
    return normalize_severity(m[1]) if m else None
