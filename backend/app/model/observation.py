"""Canonical Observation: every dataset row becomes one of these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Kind = str  # "event" | "log" | "alert" | "metric"


@dataclass
class Observation:
    timestamp: datetime
    message: str
    kind: Kind = "log"
    severity: str = "INFO"
    service: str = ""
    host: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = ""          # file name inside the upload
    line_no: int = 0
    parser: str = ""
    parser_confidence: float = 1.0
    template_id: str = ""     # filled by reduce.drain
    fingerprint: str = ""     # filled by reduce.fingerprint

    @property
    def ref(self) -> str:
        """Evidence reference shown in the UI, e.g. payment.jsonl:443."""
        return f"{self.source}:{self.line_no}"
