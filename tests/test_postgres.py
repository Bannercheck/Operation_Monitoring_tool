"""Every store against a real PostgreSQL: runs when WATCHOVER_TEST_DATABASE_URL points at a scratch database (CI starts one)."""
import os
import sqlite3
from pathlib import Path

import pytest

URL = os.environ.get("WATCHOVER_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="WATCHOVER_TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def kb():
    from watchover.db import Database
    from watchover.knowledge import Knowledge
    scratch = Database(URL)
    scratch._exec("DROP SCHEMA public CASCADE"); scratch._exec("CREATE SCHEMA public"); scratch.close()
    k = Knowledge(URL)
    yield k
    k.close()


def test_backend_and_placeholders(kb):
    assert kb.pg and kb.backend == "postgresql" and "@" not in kb.label and kb.label.startswith("postgresql · ")
    lid = kb.add("note", "pool", "connection pool exhausted, 90% used")
    assert kb.add("note", "pool", "connection pool exhausted, 90% used") == lid            # content hash dedup
    assert kb._exec("SELECT COUNT(*) AS n FROM lessons WHERE text LIKE '%pool%'")[0]["n"] == 1
    assert kb._exec("SELECT COUNT(*) AS n FROM lessons WHERE text LIKE ? ", ("%pool%",))[0]["n"] == 1   # '%' survives next to a placeholder
    assert kb.stats()["bytes"] > 0 and "lessons" in kb.tables() and "id" in kb.columns("lessons")


def test_users_roles_notify(kb):
    from watchover import auth, notify, rbac
    us = auth.Users(kb); us.bootstrap()
    assert us.count() == 1 and us.initial_password_active()
    uid = us.register("ops@example.com", "Sifre-123456", name="Ops")["id"]
    us.activate(uid)
    user = us.login("ops@example.com", "Sifre-123456")
    assert user and user["role"] == "operator" and user["id"] == uid
    assert us.login("ops@example.com", "wrong") is None and us.events(5, "ops@example.com")[0]["ok"] == 0
    code = us.otp_issue(uid)
    assert us.otp_verify(uid, code)[0]
    tok = us.remember_issue(uid, agent="pytest")
    assert us.remember_lookup(tok)["id"] == uid
    roles = rbac.Roles(kb)
    assert {r["name"] for r in roles.list()} >= {"admin", "operator", "viewer"}
    roles.save("auditor", ["page.ops", "page.data"], label="Auditor")
    assert set(roles.get("auditor")["perms"]) == {"page.data", "page.ops"}
    n = notify.Notifier(kb, {})
    rid = n.add_recipient("Nöbetçi", "oncall@example.com", "+905551112233", "ops", "")
    assert n.recipients()[0]["id"] == rid and n.recipients()[0]["enabled"] == 1
    n.update_recipient(rid, enabled=False)
    assert n.recipients()[0]["enabled"] == 0


def test_agents_sources_inventory_history(kb):
    from watchover import agents, history, inventory, sources
    reg = agents.AgentRegistry(kb)
    rec, token = reg.enroll("web-01", env="prod", site="dc1")
    aid = rec["id"]
    assert reg.verify(token)["id"] == aid and reg.active_count() == 1
    reg.touch(aid, "10.0.0.5", 12)
    assert reg.get(aid)["events"] == 12
    ss = sources.SourceStore(kb)
    src = ss.add(sources.Source(id=None, name="api", kind="loki", url="http://127.0.0.1:1/x"))
    assert ss.get(src.id).name == "api" and len(ss.list()) == 1
    inv = inventory.Inventory(kb)
    inv.upsert({"hostname": "WEB-01", "dc": "dc1", "owner": "platform"})
    assert inv.get("web-01")["owner"] == "platform"
    h = history.History(kb)
    from watchover.models import Observation
    from datetime import datetime, timezone
    t0 = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    h.add([Observation(timestamp=t0, message="x", severity="ERROR", service="api", host="web-01", environment="prod"),
           Observation(timestamp=t0, message="y", severity="INFO", service="api", host="web-01", environment="prod")])
    h.flush(); h.flush()
    cov = h.coverage()
    assert cov["rows_"] == 1 and cov["first"].startswith("2026-09-21T10:00")


def test_actions_playbook_shared_db(kb):
    from watchover.actions import ActionStore
    from watchover.analysis import Analysis
    from watchover.pipeline import ingest_path
    from watchover.playbook import Playbook
    st = ActionStore(kb)
    a = st.create("INC-1", "Restart pool", "P1", "ops")
    assert st.update(a["id"], status="done")["status"] == "done" and st.list("INC-1")[0]["id"] == a["id"]
    pb = Playbook(kb)
    obs, rep = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    an = Analysis(obs, rep)
    assert pb.record(an, "demo_mixed.zip") >= 4
    root = an.signal_by_id[an.incidents[0].root_cause_signal].template
    pb.record(an, "other.zip")
    e = pb.get(root)
    assert e["occurrences"] == 2 and e["datasets"] == ["demo_mixed.zip", "other.zip"] and e["first_seen"] <= e["last_seen"]
    assert pb.lookup(root)["template"] == root and "actions" in kb.tables() and "playbook" in kb.tables()


def test_dump_restore_and_migrate(kb, tmp_path):
    from watchover import migrate
    from watchover.actions import ActionStore
    before = kb.dump()
    assert before["actions"]["rows"] and before["users"]["columns"][:2] == ["id", "email"]
    ActionStore(kb).create("INC-2", "Extra", "P3")
    assert kb.count("actions") == 2
    done = kb.restore(before)
    assert done["actions"] == 1 and kb.count("actions") == 1
    nid = ActionStore(kb).create("INC-3", "After restore", "P2")["id"]         # sequence continues after the restored max id
    assert nid > 1
    # a classic install (three SQLite files) -> this PostgreSQL
    src = tmp_path / "old"; src.mkdir()
    from watchover.auth import Users
    from watchover.knowledge import Knowledge
    old = Knowledge(str(src / "knowledge.db")); ou = Users(old); uid = ou.register("legacy@example.com", "Sifre-123456", name="Legacy")["id"]; ou.activate(uid); old.close()
    ActionStore(str(src / "actions.db")).create("INC-OLD", "From sqlite", "P2")
    kb._exec("DELETE FROM actions")
    rep = migrate.migrate(source=str(src), target=URL)
    assert rep["tables"]["users"] >= 1 and rep["tables"]["actions"] == 1 and "playbook" not in rep["tables"]
    emails = {r["email"] for r in kb._exec("SELECT email FROM users")}
    assert {"legacy@example.com", "ops@example.com"} <= emails
    assert migrate.migrate(source=str(src), target=URL)["tables"]["actions"] == 0    # re-run copies nothing twice


def test_app_runs_on_postgres(kb, tmp_path, monkeypatch):
    """The dashboard boots with DATABASE_URL, loads the demo set and records actions / playbook in the same PostgreSQL."""
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1")
    monkeypatch.setenv("LIVE_SPOOL", str(tmp_path / "live.jsonl"))
    monkeypatch.setenv("LIVE_PORT", "18697")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    assert not at.exception
    at.sidebar.radio(key="page").set_value("data").run()
    at.button(key="demo_main").click().run()
    assert not at.exception
    assert kb.count("playbook") >= 4 and kb.count("lessons") >= 1
    at.sidebar.radio(key="page").set_value("sys").run()
    assert not at.exception
    assert any("postgresql" in m.value for m in at.markdown)
