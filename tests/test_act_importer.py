"""WP5: playbook notes -> runbook drafts (§6.10): bullets become typed or manual steps; the disk entry imports and publishes."""
import pytest

from watchover.act import ActService
from watchover.act.importer import bullets, capture, draft_from_bullets
from watchover.act.runbooks import errors, lint
from watchover.actions import open_store
from watchover.db import Database

PW = "Str0ng!Passw0rd"


@pytest.fixture
def act(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    db = Database(str(tmp_path / "k.db"))
    open_store(db, str(tmp_path / "none.db"))
    return ActService(db=db)


def test_bullets():
    assert bullets("Not\n- bir\n* iki\n1. üç\n  2) dört\nson") == ["bir", "iki", "üç", "dört"]


def test_capture_rules(act):
    tpl = act.catalogue.approved()
    assert capture("systemctl restart orders-api", tpl) == {"template": "linux.service.restart", "values": {"unit": "orders-api.service"}, "missing": False}
    assert capture("Restart-WebAppPool OrdersPool", tpl)["values"] == {"pool": "OrdersPool"}
    assert capture('appcmd recycle apppool /apppool.name:"Orders"', tpl)["template"] == "iis.apppool.recycle"
    c = capture("find /var/log/orders -name '*.log.*' -mtime +7 -exec gzip {} +", tpl)
    assert c["template"] == "linux.logs.compress_old" and c["values"] == {"path": "/var/log/orders", "days": 7}
    assert capture("logrotate -f /etc/logrotate.d/nginx", tpl)["values"] == {"conf": "nginx"}
    assert capture("journalctl --vacuum-size=500M", tpl)["values"] == {"size": "500M"}
    assert capture("kill -9 4121", tpl) == {"template": "linux.proc.kill", "missing": True, "values": {}}
    assert capture("find /etc -mtime +7 -exec gzip {} +", tpl)["values"]["path"] is None      # outside the template's roots
    assert capture("Uygulama ekibini ara", tpl) is None


def test_bullets_to_typed_and_manual_steps(act):
    tpl = act.catalogue.approved()
    items = ["df -h /var/log ile doluluğa bak", "find /var/log -name '*.log.*' -mtime +7 -exec gzip {} +", "kill -9 4121",
             "SM50'de takılı süreci iptal et", "Restart-WebAppPool OrdersPool", "Uygulama ekibine haber ver; PGPASSWORD=Canary-Env-22"]
    d = draft_from_bullets("pb-disk", "Disk", items, tpl, match_template="No space left on device")
    types = [(s["id"], s["type"], s.get("template", "")) for s in d["steps"]]
    assert types[0] == ("d1", "diagnose", "linux.disk.usage")
    assert ("a1", "act", "linux.logs.compress_old") in types and ("r1", "rollback", "linux.logs.decompress_list") in types
    assert [t for t in types if t[1] == "manual"][0][0] == "m1"
    manual = [s for s in d["steps"] if s["type"] == "manual"]
    assert len(manual) == 3 and "linux.proc.kill" in manual[0]["hint"] and manual[1]["title"] == "SAP SM50 iptal"
    assert "Canary-Env-22" not in str(d)                                   # bullets are scrubbed
    pool = next(s for s in d["steps"] if s.get("template") == "iis.apppool.recycle")
    assert d["params"][pool["args"]["pool"][1:-1]]["from"] == ["evidence.app_pool", "default"]
    assert d["params"]["days"] == {"type": "int", "min": 1, "max": 30, "from": ["default"], "default": 7}
    a1 = next(s for s in d["steps"] if s["id"] == "a1")
    assert a1["verify"] == "v1" and a1["rollback"] == "r1"


def test_disk_playbook_entry_imports_and_publishes(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    app = create_app(); svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("mgr@corp.com", PW, name="m", role="admin")
    tpl = "write failed: No space left on device (<PATH>)"
    svc.playbook.db._exec("INSERT INTO playbook (key, template, severity, title, runbook, updated_at) VALUES (?,?,?,?,?,?)",
                          (tpl, tpl, "error", "No space left on device", "- df -P /var/log\n- find /var/log -name '*.log.*' -mtime +7 -exec gzip {} +\n"
                                                                        "- Uygulama ekibine saklama süresini sorun", "x"))
    svc.act.set_mode("guided", by="admin@watchover.local")
    c = TestClient(app)
    h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Watchover!Admin2026"}).json()["token"]}
    r = c.post("/api/act/runbooks/draft-from", json={"playbook_key": tpl, "key": "pb-disk-full"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft" and errors(body["lint"]) == []
    assert [s["type"] for s in body["body"]["steps"]] == ["diagnose", "act", "verify", "rollback", "manual"]
    assert svc.act.runbooks.for_playbook(tpl) == ["pb-disk-full"]
    assert c.post("/api/act/runbooks/pb-disk-full/versions/1/submit", json={"tabletop": True}, headers=h).json()["status"] == "in_review"
    h2 = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "mgr@corp.com", "password": PW}).json()["token"]}
    r = c.post("/api/act/runbooks/pb-disk-full/versions/1/review", json={"decision": "approve"}, headers=h2)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert lint(r.json()["body"], svc.act.catalogue.approved()) is not None
