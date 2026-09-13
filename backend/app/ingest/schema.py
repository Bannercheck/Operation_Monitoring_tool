"""Schema mapper: guesses which columns/keys hold timestamp, severity, service, host, message.

Heuristic only. The UI shows the guess and lets the user override it.
"""

from __future__ import annotations

CANDIDATES = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time"),
    "severity": ("severity", "level", "loglevel", "priority", "status"),
    "service": ("service", "app", "application", "component", "source", "logger"),
    "host": ("host", "hostname", "node", "instance", "server"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title"),
}


def guess_mapping(columns: list[str]) -> dict[str, str | None]:
    lowered = {c.lower(): c for c in columns}
    mapping: dict[str, str | None] = {}
    for role, names in CANDIDATES.items():
        mapping[role] = next((lowered[n] for n in names if n in lowered), None)
    return mapping
