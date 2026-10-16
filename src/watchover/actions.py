"""Action tracker. Signal -> Investigate / Assign / Suppress / Create action; lifecycle open -> in_progress -> done.
Rows live in the shared database (PostgreSQL or the embedded SQLite file). Every caller opens the store through
`open_store(kb)`: on SQLite the rows of the old separate `actions.db` are copied into the shared database once (marker
`actions_in_kb_v1` in `rbac_meta`); the old file stays where it is, untouched, so a code rollback still finds it.

The same table holds the cases of the action agent (`kind='case'`, docs/design/action-agent.md §11.1); plain tracked actions
are `kind='action'` and legacy callers list only those."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import Database

STATUSES = ("open", "in_progress", "done", "suppressed")
PRIORITIES = ("P1", "P2", "P3", "P4")
KINDS = ("action", "case")
MARKER = "actions_in_kb_v1"

# ownership columns plus the case columns (§11.1); added with ALTER TABLE on every start, existing rows become kind='action'
EXTRA_COLS = (
    ("team_id", "INTEGER"), ("host", "TEXT DEFAULT ''"),
    ("kind", "TEXT DEFAULT 'action'"), ("case_key", "TEXT DEFAULT ''"), ("family", "TEXT DEFAULT ''"),
    ("signature", "TEXT DEFAULT ''"), ("source", "TEXT DEFAULT ''"), ("source_ref", "TEXT DEFAULT '{}'"),
    ("env", "TEXT DEFAULT ''"), ("env_tier", "TEXT DEFAULT ''"), ("pool", "TEXT DEFAULT ''"), ("host_class", "TEXT DEFAULT ''"),
    ("service", "TEXT DEFAULT ''"), ("severity", "TEXT DEFAULT ''"), ("phase", "TEXT DEFAULT ''"), ("hits", "INTEGER DEFAULT 0"),
    ("reopened", "INTEGER DEFAULT 0"), ("first_seen", "TEXT DEFAULT ''"), ("last_seen", "TEXT DEFAULT ''"),
    ("closed_at", "TEXT DEFAULT ''"), ("runbook_key", "TEXT DEFAULT ''"), ("run_id", "INTEGER"), ("level_hint", "INTEGER"),
    ("next_step", "TEXT DEFAULT ''"), ("needs_human", "INTEGER DEFAULT 0"), ("outcome", "TEXT DEFAULT ''"),
    ("label", "TEXT DEFAULT ''"), ("label_final", "INTEGER DEFAULT 0"), ("resolution", "TEXT DEFAULT ''"),
    ("parent_id", "INTEGER"), ("prev_id", "INTEGER"), ("suppressed_until", "TEXT DEFAULT ''"), ("blind", "INTEGER DEFAULT 0"),
    ("peeked", "INTEGER DEFAULT 0"), ("injection_suspect", "INTEGER DEFAULT 0"), ("provenance", "TEXT DEFAULT ''"),
    ("ood", "INTEGER DEFAULT 0"), ("storm", "INTEGER DEFAULT 0"),
    ("demo", "INTEGER DEFAULT 0"),                 # 1 = made from demo input (a demo dataset's first action, an action of a demo anomaly; demo.py)
)
INDEXES = (("ix_actions_kind", "kind, team_id, phase"), ("ix_actions_case_key", "case_key, phase"), ("ix_actions_host", "host, phase"))


class ActionStore:
    def __init__(self, db: "Database | str" = "actions.db"):
        self.db = db if isinstance(db, Database) else Database(db)
        self.db._exec(f"""CREATE TABLE IF NOT EXISTS actions (
            id {self.db.pk}, incident_id TEXT, title TEXT, priority TEXT DEFAULT 'P2',
            status TEXT DEFAULT 'open', owner TEXT DEFAULT '', recommendation TEXT DEFAULT '', evidence TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT)""")
        self._ensure_cols()

    def _ensure_cols(self) -> None:
        have = set(self.db.columns("actions"))
        for col, ddl in EXTRA_COLS:
            if col not in have:
                try:
                    self.db._exec(f"ALTER TABLE actions ADD COLUMN {col} {ddl}")
                except Exception:  # noqa: BLE001 - another process added it first
                    pass
        for name, cols in INDEXES:
            try:
                self.db._exec(f"CREATE INDEX IF NOT EXISTS {name} ON actions ({cols})")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def list(self, incident_id: str | None = None, kind: str = "action") -> list[dict]:
        """Plain actions by default (legacy callers never see cases); kind='case' for cases, kind='all' for both."""
        where, args = [], []
        if incident_id:
            where.append("incident_id=?"); args.append(incident_id)
        if kind and kind != "all":
            where.append("COALESCE(kind, 'action')=?"); args.append(kind)
        q = "SELECT * FROM actions" + (" WHERE " + " AND ".join(where) if where else "")
        return self.db._exec(q + " ORDER BY id", tuple(args))

    def create(self, incident_id: str, title: str, priority: str = "P2", owner: str = "",
               recommendation: str = "", evidence: str = "", demo: bool = False) -> dict:
        if priority not in PRIORITIES:
            raise ValueError("bad priority")
        now = self._now()
        aid = self.db._insert(
            "INSERT INTO actions (incident_id,title,priority,owner,recommendation,evidence,created_at,updated_at,demo) VALUES (?,?,?,?,?,?,?,?,?)",
            (incident_id, title, priority, owner, recommendation, evidence, now, now, int(bool(demo))))
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


# ---------------------------------------------------------------- the one opener (api/services.py, mcp_server.py, app.py, migrate.py)
def _meta(kb: Database, key: str) -> str | None:
    kb._exec("CREATE TABLE IF NOT EXISTS rbac_meta (key TEXT PRIMARY KEY, value TEXT)")
    rows = kb._exec("SELECT value FROM rbac_meta WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _legacy_rows(path: Path) -> tuple[list[str], list[list]]:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)      # read-only: the old file is never written again
    try:
        if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='actions'").fetchone():
            return [], []
        cols = [r[1] for r in src.execute("PRAGMA table_info(actions)")]
        rows = [list(r) for r in src.execute(f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} FROM actions")]
        return cols, rows
    finally:
        src.close()


def move_into_kb(kb: Database, legacy_path: str | None = None) -> dict | None:
    """One-shot copy of the old `actions.db` rows (ids, team_id, host kept) into the shared SQLite database. Idempotent:
    the marker prevents a second copy; nothing is copied when the shared table already has rows. Returns the report the first time."""
    if kb.pg or _meta(kb, MARKER):
        return None
    path = Path(legacy_path or os.environ.get("ACTIONS_DB", "actions.db"))
    cols, rows = [], []
    if path.exists() and path.suffix == ".db" and not _same_file(str(path), kb.url):
        try:
            cols, rows = _legacy_rows(path)
        except sqlite3.Error:
            cols, rows = [], []
    rep = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": str(path) if cols else "", "rows": 0}
    with kb.tx():                                      # check, copy and marker together: a second process starting now copies nothing
        if kb._exec("SELECT value FROM rbac_meta WHERE key=?", (MARKER,)):
            return None
        if cols and rows and kb.count("actions") == 0:
            rep["rows"] = kb.copy_table("actions", cols, rows)
        kb._exec("INSERT INTO rbac_meta (key, value) VALUES (?,?)", (MARKER, json.dumps(rep)))
    return rep


def open_store(kb: Database, legacy_path: str | None = None) -> ActionStore:
    """The action store in the shared database; on SQLite the old separate file is copied in once first."""
    store = ActionStore(kb)
    if not kb.pg:
        move_into_kb(kb, legacy_path)
    return store


def moved_in(sqlite_path) -> bool:
    """True when this SQLite knowledge file already holds the actions (the marker exists): `migrate` then skips actions.db."""
    try:
        src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            return bool(src.execute("SELECT 1 FROM rbac_meta WHERE key=?", (MARKER,)).fetchone())
        finally:
            src.close()
    except sqlite3.Error:
        return False
