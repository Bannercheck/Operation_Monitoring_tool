"""Move an installation from the embedded SQLite files to PostgreSQL.

    python -m watchover.migrate                       # knowledge.db + actions.db + playbook.db (or the *_DB env paths) -> DATABASE_URL
    python -m watchover.migrate --source /data --target postgresql://... [--dry-run]

Every table found in the SQLite files is copied into the same table in PostgreSQL (the schema is created first by opening every
store). Rows whose key already exists are skipped, so the command can be re-run; move into an empty database to keep every id. The app
runs the same thing by itself the first time it starts on PostgreSQL and finds SQLite files next to its data: `auto()`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import Database, is_postgres, q

FILES = (("KNOWLEDGE_DB", "knowledge.db"), ("ACTIONS_DB", "actions.db"), ("PLAYBOOK_DB", "playbook.db"))
SKIP = ("otp_codes", "remember_tokens")            # sign-in leftovers: everyone signs in again after the move
MARKER = ".migrated-to-postgres"


def sqlite_files(source: str | None = None) -> list[Path]:
    """The SQLite files of a classic install: explicit folder, else the env paths, else the working folder."""
    out = []
    for env, name in FILES:
        cands = [Path(source) / name] if source else [Path(os.environ.get(env, name)), Path(os.environ.get("WATCHOVER_HOME", "data")) / name, Path(name)]
        for p in cands:
            if p.exists() and p.suffix == ".db" and not is_postgres(str(p)) and p not in out:
                out.append(p); break
    return out


def open_schema(target: Database) -> None:
    """Create every table Watchover uses so the copy has somewhere to land (imports are local: the stores import this module's neighbours)."""
    from . import agents, auth, history, inventory, notify, rbac, sources
    from .actions import ActionStore
    from .knowledge import Knowledge
    from .playbook import Playbook
    kb = target if isinstance(target, Knowledge) else Knowledge(target.url)
    auth.Users(kb); rbac.Roles(kb); notify.Notifier(kb); agents.AgentRegistry(kb); sources.SourceStore(kb); inventory.Inventory(kb)
    history.History(kb)
    kb._exec(f"CREATE TABLE IF NOT EXISTS learn_runs (id {kb.pk}, ts TEXT, events INTEGER, incidents INTEGER, lessons INTEGER, note TEXT DEFAULT '')")   # autolearn
    ActionStore(kb); Playbook(kb)


def _fresh_users(target: Database) -> bool:
    """Only the bootstrap admin, never signed in: safe to replace with the accounts from SQLite."""
    rows = target._exec("SELECT last_login, must_change FROM users")
    return len(rows) <= 1 and all(not r["last_login"] and r["must_change"] for r in rows)


def migrate(source: str | None = None, target: str | None = None, dry_run: bool = False) -> dict:
    url = target or os.environ.get("DATABASE_URL", "")
    if not is_postgres(url):
        raise SystemExit("target must be a postgresql:// URL (DATABASE_URL)")
    files = sqlite_files(source)
    report = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "target": url.split("@")[-1], "files": [str(p) for p in files], "tables": {}}
    if not files:
        return report
    db = Database(url)
    open_schema(db)
    have = set(db.tables())
    for p in files:
        src = sqlite3.connect(str(p)); src.row_factory = sqlite3.Row
        for t in [r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]:
            if t not in have or t in SKIP:
                continue
            cols = [r["name"] for r in src.execute(f"PRAGMA table_info({t})")]
            rows = [[bytes(v) if isinstance(v, memoryview) else v for v in r] for r in src.execute(f"SELECT {', '.join(q(cols))} FROM {t}")]
            if dry_run:
                report["tables"][t] = len(rows); continue
            if t == "users" and rows:
                if _fresh_users(db):
                    db._exec("DELETE FROM users")               # only the untouched bootstrap admin: the SQLite accounts replace it, ids kept
                elif "id" in cols:                              # accounts already exist here: new e-mails are appended with fresh ids
                    i = cols.index("id"); cols = cols[:i] + cols[i + 1:]; rows = [r[:i] + r[i + 1:] for r in rows]
            report["tables"][t] = db.copy_table(t, cols, rows)   # keys that already exist are skipped: re-running copies nothing twice
        src.close()
    db.close()
    return report


def auto(kb) -> dict | None:
    """First start on PostgreSQL: if SQLite files are lying next to the data folder and the copy never ran, copy them once."""
    if not kb.pg:
        return None
    home = Path(os.environ.get("WATCHOVER_HOME") or "data")
    marker = home / MARKER
    if marker.exists() or not sqlite_files():
        return None
    try:
        rep = migrate(target=kb.url)
    except Exception as e:  # noqa: BLE001 - never block the app on a failed import; the CLI reports the detail
        rep = {"error": str(e)[:300]}
    home.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.migrate", description="copy the SQLite files of a classic install into PostgreSQL")
    ap.add_argument("--source", help="folder holding knowledge.db / actions.db / playbook.db (default: env paths, then ./data, then .)")
    ap.add_argument("--target", help="postgresql://... (default: DATABASE_URL)")
    ap.add_argument("--dry-run", action="store_true", help="count rows, write nothing")
    a = ap.parse_args(argv)
    rep = migrate(a.source, a.target, a.dry_run)
    if not rep["files"]:
        print("no SQLite files found - nothing to migrate"); return 0
    print(f"{'would copy' if a.dry_run else 'copied'} into {rep['target']} from {', '.join(rep['files'])}")
    for t, n in rep["tables"].items():
        print(f"  {t:<20} {n:>7} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
