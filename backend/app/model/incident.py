"""Incident candidate: correlated signals with a scored rationale."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .signal import Signal


@dataclass
class Factor:
    name: str
    weight: float
    value: str
    contribution: float


@dataclass
class Rationale:
    score: float
    factors: list[Factor]
    evidence: list[str]
    narrative: str = ""


@dataclass
class Incident:
    id: str
    title: str
    signals: list[Signal]
    root_cause_signal_id: str
    affected_services: list[str]
    started_at: datetime
    ended_at: datetime
    rationale: Rationale
    severity: str = "medium"
    timeline: list[dict] = field(default_factory=list)
