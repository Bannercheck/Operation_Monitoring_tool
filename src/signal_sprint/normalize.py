"""Normalisation: timestamps (dateutil), severity, and the schema auto-mapper (column -> role)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dtparser

from .models import SEV_RANK

UTC = timezone.utc

SEVERITY_MAP = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "informational": "INFO", "notice": "INFO", "ok": "INFO",
    "warn": "WARN", "warning": "WARN", "error": "ERROR", "err": "ERROR", "critical": "CRITICAL", "crit": "CRITICAL",
    "fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL", "severe": "CRITICAL",
    "high": "ERROR", "medium": "WARN", "low": "INFO", "p1": "CRITICAL", "p2": "ERROR", "p3": "WARN", "p4": "INFO",
    "firing": "ERROR", "resolved": "INFO", "major": "ERROR", "minor": "WARN",
}
ROLE_HINTS = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created_at", "logged_at", "t", "start_time"),
    "severity": ("severity", "level", "loglevel", "log_level", "priority", "sev", "status"),
    "service": ("service", "app", "application", "component", "logger", "source", "module", "job", "program", "svc", "service_name"),
    "host": ("host", "hostname", "node", "instance", "server", "pod", "container", "machine"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title", "body", "line", "event", "alert", "alertname"),
}
LEVEL_WORD_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|ALERT|EMERG)\b", re.I)
SYSLOG_TS_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}$")
_DEFAULT = datetime(datetime.now().year, 1, 1)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse epoch s/ms, ISO, syslog, locale-ish strings via dateutil. Always tz-aware (naive -> UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=UTC)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=UTC)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=UTC)
    try:
        dt = dtparser.parse(s.replace(",", ".", 1) if re.search(r"\d,\d{3}$", s) else s, default=_DEFAULT,
                            dayfirst=bool(re.match(r"\d{2}[./]\d{2}[./]\d{4}", s)))
    except (ValueError, OverflowError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def normalize_severity(value: Any) -> str:
    if value is None:
        return "INFO"
    s = str(value).strip().lower()
    if s in SEVERITY_MAP:
        return SEVERITY_MAP[s]
    if s.isdigit():
        return {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR", 4: "WARN"}.get(int(s), "INFO")
    up = s.upper()
    return up if up in SEV_RANK else "INFO"


def severity_from_text(text: str) -> str:
    m = LEVEL_WORD_RE.search(text)
    return normalize_severity(m[1]) if m else "INFO"


def auto_map(keys: list[str], sample: list[dict] | None = None, mapping: dict | None = None) -> dict[str, str | None]:
    """Schema auto-mapper: which key plays timestamp / severity / service / host / message.

    Order: explicit mapping > exact name hint > suffix hint > value-based guess (timestamp-looking, level-looking, longest text).
    """
    lowered = {k.lower(): k for k in keys}
    out: dict[str, str | None] = {}
    for role, hints in ROLE_HINTS.items():
        if mapping and mapping.get(role) in keys:
            out[role] = mapping[role]
            continue
        hit = next((lowered[h] for h in hints if h in lowered), None)
        if hit is None:
            hit = next((k for lk, k in lowered.items()
                        if any(lk.endswith(sep + h) for h in hints for sep in ("_", ".", "-"))), None)
        out[role] = hit
    if sample:
        taken = {v for v in out.values() if v}
        def share(k, pred):
            vals = [r.get(k) for r in sample if r.get(k) not in (None, "")]
            return sum(1 for v in vals if pred(v)) / len(vals) if vals else 0.0
        if out["timestamp"] is None:
            best = max((k for k in keys if k not in taken), key=lambda k: share(k, lambda v: parse_timestamp(v) is not None and len(str(v)) >= 8), default=None)
            if best and share(best, lambda v: parse_timestamp(v) is not None and len(str(v)) >= 8) > 0.8:
                out["timestamp"] = best; taken.add(best)
        if out["severity"] is None:
            best = max((k for k in keys if k not in taken), key=lambda k: share(k, lambda v: str(v).lower() in SEVERITY_MAP), default=None)
            if best and share(best, lambda v: str(v).lower() in SEVERITY_MAP) > 0.8:
                out["severity"] = best; taken.add(best)
        if out["message"] is None:
            cands = [k for k in keys if k not in taken]
            best = max(cands, key=lambda k: sum(len(str(r.get(k, ""))) for r in sample), default=None)
            out["message"] = best
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out
