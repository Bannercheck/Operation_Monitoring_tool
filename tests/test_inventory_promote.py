"""Discovered hosts at fleet scale: each one carries its pool (the name stem, since the inventory has no cluster for it yet),
a whole pool or a selection is promoted in one call, the call is guarded by act.inventory like every inventory write, and a
promoted host leaves the discovered list."""
import json
from datetime import datetime, timezone

import pytest

UTC = timezone.utc
HOSTS = ["prd-sap-app-01", "prd-sap-app-02", "prd-sap-app-03", "web07", "10.9.8.7"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("view@corp.com", "Str0ng!Passw0rd", role="viewer")
    now = datetime.now(UTC).isoformat()
    lines = [{"ts": now, "level": "info", "service": "app", "host": h, "env": "prod", "msg": "GET /health 200 12ms"} for h in HOSTS]
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode(), "agent")
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email="admin@watchover.local", pw="Test-Yonetici-Parola-42"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_discovered_carries_pool(app):
    rows = {r["hostname"]: r for r in app.get("/api/inventory/discovered", headers=login(app)).json()}
    assert set(HOSTS) <= set(rows)
    assert {rows[h]["pool"] for h in HOSTS[:3]} == {"prd-sap-app"} and rows["web07"]["pool"] == "web" and rows["10.9.8.7"]["pool"] == "-"
    r = rows["prd-sap-app-01"]                                           # the fields the pages used before stay
    assert r["env"] == "prod" and r["source"] == "discovered" and "ip" in r and rows["10.9.8.7"]["ip"] == "10.9.8.7"


def test_bulk_promote_and_list_shrinks(app):
    h = login(app)
    pool = [{"hostname": x["hostname"], "env": x["env"], "ip": x["ip"], "dc": x.get("dc", "")}
            for x in app.get("/api/inventory/discovered", headers=h).json() if x["pool"] == "prd-sap-app"]
    r = app.post("/api/inventory/promote", json={"hosts": pool, "criticality": "high"}, headers=h)
    assert r.status_code == 200 and r.json() == {"added": 3}
    rec = app.get("/api/inventory/prd-sap-app-02", headers=h).json()
    assert rec["criticality"] == "high" and rec["env"] == "prod" and rec["source"] == "discovered"
    left = {x["hostname"] for x in app.get("/api/inventory/discovered", headers=h).json()}
    assert not left & {"prd-sap-app-01", "prd-sap-app-02", "prd-sap-app-03"} and {"web07", "10.9.8.7"} <= left


def test_promote_validates_before_writing_and_keeps_typed_fields(app):
    h = login(app)
    bad = app.post("/api/inventory/promote", json={"hosts": [{"hostname": "web07"}, {"hostname": " "}]}, headers=h)
    assert bad.status_code == 400 and "hostname" in bad.text
    assert app.get("/api/inventory/web07", headers=h).status_code == 404          # the valid row was not written either
    app.put("/api/inventory", json={"hostname": "db-01", "owner": "DBA", "criticality": "critical"}, headers=h)
    assert app.post("/api/inventory/promote", json={"hosts": [{"hostname": "DB-01", "env": "prod", "ip": "10.0.0.1"}], "criticality": "low"}, headers=h).json() == {"added": 1}
    rec = app.get("/api/inventory/db-01", headers=h).json()
    assert rec["owner"] == "DBA" and rec["criticality"] == "critical" and rec["env"] == "prod" and rec["ip"] == "10.0.0.1"


def test_viewer_cannot_promote(app):
    v = login(app, "view@corp.com", "Str0ng!Passw0rd")
    r = app.post("/api/inventory/promote", json={"hosts": [{"hostname": "web07"}], "criticality": "normal"}, headers=v)
    assert r.status_code == 403
    assert app.get("/api/inventory/web07", headers=login(app)).status_code == 404


def test_team_sees_and_promotes_only_its_hosts(app):
    admin = login(app)
    app.svc.users.register("sap@corp.com", "Str0ng!Passw0rd", role="operator")
    r = app.post("/api/teams", json={"name": "SAP", "rules": [{"field": "host", "op": "glob", "value": "prd-sap-*"}], "members": ["sap@corp.com"]}, headers=admin)
    assert r.status_code in (200, 201), r.text
    h = login(app, "sap@corp.com", "Str0ng!Passw0rd")
    assert {x["hostname"] for x in app.get("/api/inventory/discovered", headers=h).json()} == set(HOSTS[:3])   # not web07 / the IP
    assert app.post("/api/inventory/promote", json={"hosts": [{"hostname": "web07"}]}, headers=h).status_code == 403
    assert app.get("/api/inventory/web07", headers=admin).status_code == 404
    assert app.post("/api/inventory/promote", json={"hosts": [{"hostname": "prd-sap-app-01", "env": "prod"}]}, headers=h).json() == {"added": 1}
