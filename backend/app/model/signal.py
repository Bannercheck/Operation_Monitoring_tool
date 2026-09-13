"""Signal: a group of observations sharing a fingerprint, with burst stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .observation import Observation


@dataclass
class Signal:
    id: str
    fingerprint: str
    template: str
    count: int
    severity: str
    services: list[str]
    hosts: list[str]
    first_seen: datetime
    last_seen: datetime
    peak_rate_per_min: float = 0.0
    baseline_rate_per_min: float = 0.0
    burst_score: float = 0.0
    onset: datetime | None = None
    observations: list[Observation] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        return [o.ref for o in self.observations[:20]]
