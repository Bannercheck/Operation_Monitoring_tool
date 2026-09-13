"""Action item tracked against an incident."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUSES = ("open", "in_progress", "done")


@dataclass
class Action:
    id: int
    incident_id: str
    title: str
    status: str = "open"
    assignee: str = ""
    note: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
