"""WP0b: actions move into the shared database (open_store), case columns and the kind filter."""
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watchover import actions as wo_actions
from watchover.actions import MARKER, ActionStore, moved_in, open_store
from watchover.db import Database

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"email": "admin@watchover.local", "password": "Watchover!Admin2026"}


def _legacy(path: Path) -> list[int]:
    st = ActionStore(Database(str(path)))      # an old install: the separate actions.db with team / host stamped by ownership
    a = st.create("INC-1", "Disk temizliği", "P1", "ops")
    b = st.create("INC-2", "Servis", "P3", evidence="first:demo:INC-2")
    st.delete(st.create("INC-X", "gone")["id"])   # a gap in the ids must survive the copy
    c = st.create("INC-3", "Son")
    st.db._exec("UPDATE actions SET team_id=?, host=? WHERE id=?", (7, "web-01", a["id"]))
    st.db.close()
    return [a["id"], b["id"], c["id"]]


def test_copy_once_keeps_ids_team_host(tmp_path):
    legacy = tmp_path / "actions.db"
    ids = _legacy(legacy)
    before = (legacy.stat().st_size, legacy.stat().st_mtime_ns)
    kb = Database(str(tmp_path / "knowledge.db"))
    st = open_store(kb, str(legacy))
    rows = st.list(kind="all")
    assert [r["id"] for r in rows] == ids
    first = rows[0]
    assert (first["team_id"], first["host"], first["title"]) == (7, "web-01", "Disk temizliği")
    assert all(r["kind"] == "action" for r in rows)                     # existing rows, incl. the dataset's first action, stay plain
    assert st.get(ids[1])["evidence"].startswith("first:") and st.get(ids[1])["kind"] == "action"
    assert legacy.exists() and (legacy.stat().st_size, legacy.stat().st_mtime_ns) == before   # not renamed, not written
    assert kb._exec("SELECT value FROM rbac_meta WHERE key=?", (MARKER,))
    # a later row in the old file (e.g. a stale process) is never copied: the marker prevents a second copy
    ActionStore(Database(str(legacy))).create("INC-9", "late")
    kb._exec("DELETE FROM actions WHERE id=?", (ids[2],))
    open_store(kb, str(legacy))
    assert [r["id"] for r in st.list(kind="all")] == ids[:2]
    new = st.create("INC-4", "Yeni")
    assert new["id"] > ids[-1] and new["kind"] == "action"


def test_no_legacy_file_sets_marker(tmp_path):
    kb = Database(str(tmp_path / "knowledge.db"))
    st = open_store(kb, str(tmp_path / "missing.db"))
    assert st.list() == [] and not (tmp_path / "missing.db").exists()
    assert moved_in(tmp_path / "knowledge.db")


def test_kind_filter_and_case_columns(tmp_path):
    st = open_store(Database(str(tmp_path / "k.db")), str(tmp_path / "none.db"))
    cols = set(st.db.columns("actions"))
    assert {c for c, _ in wo_actions.EXTRA_COLS} <= cols
    a = st.create("INC-1", "plain")
    cid = st.db._insert("INSERT INTO actions (incident_id, title, kind, phase, status) VALUES (?,?,?,?,?)", ("", "case", "case", "new", "open"))
    assert [r["id"] for r in st.list()] == [a["id"]]
    assert [r["id"] for r in st.list(kind="case")] == [cid]
    assert {r["id"] for r in st.list(kind="all")} == {a["id"], cid}
    ActionStore(st.db)                                                  # reopening is idempotent
    assert len(st.db.columns("actions")) == len(cols)


def test_migrate_skips_actions_db_after_marker(tmp_path, monkeypatch):
    from watchover import migrate
    _legacy(tmp_path / "actions.db")
    for k in ("KNOWLEDGE_DB", "ACTIONS_DB", "PLAYBOOK_DB"):
        monkeypatch.delenv(k, raising=False)
    Database(str(tmp_path / "knowledge.db"))._exec("CREATE TABLE t (x INTEGER)")
    names = [p.name for p in migrate.sqlite_files(str(tmp_path))]
    assert names == ["knowledge.db", "actions.db"]
    open_store(Database(str(tmp_path / "knowledge.db")), str(tmp_path / "actions.db"))
    assert [p.name for p in migrate.sqlite_files(str(tmp_path))] == ["knowledge.db"]


def test_snapshot_rollback_keeps_actions(tmp_path, monkeypatch):
    from watchover import admin
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
    _legacy(tmp_path / "a.db")
    kb = Database(str(tmp_path / "k.db"))
    st = open_store(kb)
    keep = st.create("INC-5", "snapshot öncesi")
    snap = admin.snapshot("test", code=False, db=True)
    st.delete(keep["id"])
    kb.close()
    ok, msg = admin.rollback(snap["id"], code=False, db=True)
    assert ok, msg
    st2 = open_store(Database(str(tmp_path / "k.db")))
    assert keep["id"] in {r["id"] for r in st2.list()}
    assert len(st2.list(kind="all")) == 4


def test_api_and_mcp_share_the_database(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"),
                 "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from watchover.api import create_app
    app = create_app()
    svc = app.state.services
    assert svc.actions.db is svc.kb                                    # one database for actions and everything else
    sys.path.insert(0, str(ROOT))
    import mcp_server
    importlib.reload(mcp_server)                                       # opens the same KNOWLEDGE_DB through open_store
    made = mcp_server.create_action("INC-M", "MCP aksiyonu", "P2", "ops")
    assert not Path(tmp_path / "a.db").exists()                        # no empty actions.db is created any more
    c = TestClient(app)
    svc.kb._exec("UPDATE users SET must_change=0 WHERE email=?", (ADMIN["email"],))
    h = {"Authorization": f"Bearer {c.post('/api/auth/login', json=ADMIN).json()['token']}"}
    ids = [a["id"] for a in c.get("/api/actions", headers=h).json()]
    assert made["id"] in ids
    cid = svc.kb._insert("INSERT INTO actions (incident_id, title, kind, phase, status) VALUES (?,?,?,?,?)", ("", "vaka", "case", "new", "open"))
    assert cid not in [a["id"] for a in c.get("/api/actions", headers=h).json()]
    assert cid in [a["id"] for a in c.get("/api/actions?kind=all", headers=h).json()]
    assert c.get("/api/actions?kind=bogus", headers=h).status_code == 400
    mcp_server.STORE.db.close()


@pytest.mark.skipif(not os.environ.get("WATCHOVER_TEST_DATABASE_URL"), reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_postgres_open_store():
    kb = Database(os.environ["WATCHOVER_TEST_DATABASE_URL"])
    st = open_store(kb)
    assert "kind" in kb.columns("actions")
    a = st.create("INC-PG", "pg")
    assert a["kind"] == "action" and a["id"] in [r["id"] for r in st.list()]
    st.delete(a["id"])
