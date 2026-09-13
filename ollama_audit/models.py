"""Data model for SAP Security Audit Log events and detector findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    """One line of an SAP Security Audit Log (SM20 / RSAU_READ_LOG export)."""

    timestamp: datetime
    user: str
    event_id: str  # SAP audit message id, e.g. AU1, AU2, AU3, AUW
    client: str = "000"
    terminal: str = ""
    tcode: str = ""  # transaction code, e.g. SU01, PFCG
    message: str = ""
    line_no: int = 0

    @property
    def key(self) -> str:
        return f"{self.line_no}:{self.user}:{self.event_id}"


@dataclass
class Finding:
    """One anomaly raised by a detector."""

    rule: str
    severity: str  # low | medium | high | critical
    user: str
    summary: str
    events: list[AuditEvent] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def first_seen(self) -> datetime | None:
        return min((e.timestamp for e in self.events), default=None)

    @property
    def last_seen(self) -> datetime | None:
        return max((e.timestamp for e in self.events), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "user": self.user,
            "summary": self.summary,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "event_lines": [e.line_no for e in self.events],
            "details": self.details,
        }


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
