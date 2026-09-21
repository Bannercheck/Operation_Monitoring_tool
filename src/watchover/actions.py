"""Action tracker. Signal -> Investigate / Assign / Suppress / Create action; lifecycle open -> in_progress -> done.
Rows live in the shared database (PostgreSQL or the embedded SQLite file); a path string opens a standalone SQLite file."""

from __future__ import annotations

from datetime import datetime, timezone

from .db import Database

STATUSES = ("open", "in_progress", "done", "suppressed")
PRIORITIES = ("P1", "P2", "P3", "P4")


class ActionStore:
    def __init__(self, db: "Database | str" = "actions.db"):
        self.db = db if isinstance(db, Database) else Database(db)
        self.db._exec(f"""CREATE TABLE IF NOT EXISTS actions (
            id {self.db.pk}, incident_id TEXT, title TEXT, priority TEXT DEFAULT 'P2',
            status TEXT DEFAULT 'open', owner TEXT DEFAULT '', recommendation TEXT DEFAULT '', evidence TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT)""")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def list(self, incident_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM actions", ()
        if incident_id:
            q, args = q + " WHERE incident_id=?", (incident_id,)
        return self.db._exec(q + " ORDER BY id", args)

    def create(self, incident_id: str, title: str, priority: str = "P2", owner: str = "",
               recommendation: str = "", evidence: str = "") -> dict:
        if priority not in PRIORITIES:
            raise ValueError("bad priority")
        now = self._now()
        aid = self.db._insert(
            "INSERT INTO actions (incident_id,title,priority,owner,recommendation,evidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (incident_id, title, priority, owner, recommendation, evidence, now, now))
        return self.get(aid)

    def update(self, action_id: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("title", "status", "owner", "priority", "recommendation") and v is not None}
        if allowed.get("status", "open") not in STATUSES or allowed.get("priority", "P2") not in PRIORITIES:
            raise ValueError("bad status or priority")
        allowed["updated_at"] = self._now()
        self.db._exec(f"UPDATE actions SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), action_id))
        return self.get(action_id)

    def delete(self, action_id: int) -> None:
        self.db._exec("DELETE FROM actions WHERE id=?", (action_id,))

    def get(self, action_id: int) -> dict:
        rows = self.db._exec("SELECT * FROM actions WHERE id=?", (action_id,))
        if not rows:
            raise KeyError(action_id)
        return rows[0]
