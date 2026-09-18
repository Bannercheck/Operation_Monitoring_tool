"""Canonical data model. Every dataset row becomes an Observation; everything downstream is dataset-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SEV_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}


@dataclass
class Observation:
    timestamp: datetime
    message: str
    kind: str = "log"                 # log | event | alert | metric | incident
    severity: str = "INFO"
    service: str = ""
    host: str = ""
    environment: str = ""             # prod | test | dev | staging | uat | qa | dr | "" (from a column, or inferred from host/message)
    origin: str = ""                  # where the error came from: source / component / subsystem / category column
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = ""                  # file name inside the upload
    line_no: int = 0
    parser: str = ""
    parser_confidence: float = 1.0
    template: str = ""                # volatile values masked, filled by analysis
    fingerprint: str = ""             # sha1(template|severity|service), filled by analysis
    raw: str = ""                     # the original line / record text as it appears in the dataset

    @property
    def ref(self) -> str:
        """Evidence reference shown in the UI: file:line."""
        return f"{self.source}:{self.line_no}"


@dataclass
class Signal:
    """Observations sharing one fingerprint, with burst statistics and a rationale."""
    id: str
    fingerprint: str
    template: str
    severity: str
    count: int
    services: list[str]
    hosts: list[str]
    entities: set[str]
    first_seen: datetime
    last_seen: datetime
    onset: datetime
    peak_rate: float = 0.0
    baseline_rate: float = 0.0
    burst_score: float = 0.0
    observations: list[Observation] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        return [o.ref for o in self.observations[:10]]

    def why(self) -> dict:
        """WHY THIS SIGNAL? Evidence + reason + confidence for the grouping decision."""
        sources = sorted({o.source for o in self.observations})
        return {
            "dataset": sources,
            "records": [o.line_no for o in self.observations[:10]],
            "reason": "same normalized message fingerprint (volatile values masked)",
            "template": self.template,
            "grouping": {"severity": self.severity, "services": self.services,
                         "window": f"{self.first_seen:%H:%M:%S} - {self.last_seen:%H:%M:%S}"},
            "burst": {"peak_per_min": self.peak_rate, "baseline_per_min": self.baseline_rate, "score": self.burst_score},
            "confidence": round(min(1.0, 0.6 + 0.4 * min(self.count, 10) / 10), 2),
        }


@dataclass
class Factor:
    name: str
    weight: float
    value: str                      # English label, used by CLI / postmortem
    contribution: float
    data: dict = field(default_factory=dict)   # raw numbers for localized rendering


@dataclass
class Incident:
    id: str
    title: str
    severity: str
    score: float
    root_cause_signal: str
    root_cause_reason: str
    affected_services: list[str]
    affected_hosts: list[str]
    started_at: datetime
    ended_at: datetime
    signal_ids: list[str]
    factors: list[Factor]
    evidence: list[str]
    timeline: list[dict]
    links: list[dict]
    narrative: str
    recommendations: list[str] = field(default_factory=list)
    root_cause_codes: list = field(default_factory=list)   # i18n reason codes, e.g. ["r_earliest", ("r_fan", 3)]
    root_cause_alternatives: list = field(default_factory=list)   # [{"signal": id, "score": float, "codes": [...]}] runner-up hypotheses
    origin: dict = field(default_factory=dict)             # where: {"files": {name: count}, "services": [...], "hosts": [...], "agents": [...]}
    timing: dict = field(default_factory=dict)             # first_signal, last_error, duration_s, quiet_s, dataset_end
    recovery: dict = field(default_factory=dict)           # kind: self_healed | restart | ongoing | unknown; recovered_at; evidence; what


@dataclass
class Action:
    id: int
    incident_id: str
    title: str
    priority: str = "P2"              # P1..P4
    status: str = "open"              # open | in_progress | done | suppressed
    owner: str = ""
    recommendation: str = ""
    evidence: str = ""
    created_at: str = ""
    updated_at: str = ""
