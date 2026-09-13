"""Action tracker (SQLite). Signal -> Investigate / Assign / Suppress / Create action; lifecycle open -> in_progress -> done."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

STATUSES = ("open", "in_progress", "done", "suppressed")
PRIORITIES = ("P1", "P2", "P3", "P4")


class ActionStore:
    def __init__(self, path: str = "actions.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, title TEXT, priority TEXT DEFAULT 'P2',
            status TEXT DEFAULT 'open', owner TEXT DEFAULT '', recommendation TEXT DEFAULT '', evidence TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT)""")
        self.conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def list(self, incident_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM actions", ()
        if incident_id:
            q, args = q + " WHERE incident_id=?", (incident_id,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY id", args)]

    def create(self, incident_id: str, title: str, priority: str = "P2", owner: str = "",
               recommendation: str = "", evidence: str = "") -> dict:
        if priority not in PRIORITIES:
            raise ValueError("bad priority")
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO actions (incident_id,title,priority,owner,recommendation,evidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (incident_id, title, priority, owner, recommendation, evidence, now, now))
        self.conn.commit()
        return self.get(cur.lastrowid)

    def update(self, action_id: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("title", "status", "owner", "priority", "recommendation") and v is not None}
        if allowed.get("status", "open") not in STATUSES or allowed.get("priority", "P2") not in PRIORITIES:
            raise ValueError("bad status or priority")
        allowed["updated_at"] = self._now()
        self.conn.execute(f"UPDATE actions SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), action_id))
        self.conn.commit()
        return self.get(action_id)

    def delete(self, action_id: int) -> None:
        self.conn.execute("DELETE FROM actions WHERE id=?", (action_id,))
        self.conn.commit()

    def get(self, action_id: int) -> dict:
        r = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if r is None:
            raise KeyError(action_id)
        return dict(r)
