"""Teams: several teams on one Watchover, each member sees only the team's hosts (rules + explicit hosts), across the live
feed, anomalies, APM, user experience, integrations, inventory and agents; admins and scope.all see everything; users in no
team see everything unless teams_strict is on."""
import json
import random
from datetime import datetime, timezone

import pytest

UTC = timezone.utc


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
    for email, role in (("ea@corp.com", "operator"), ("hr@corp.com", "operator"), ("noc@corp.com", "operator"), ("free@corp.com", "viewer")):
        svc.users.register(email, "Str0ng!Passw0rd", role=role)
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw="Str0ng!Passw0rd"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def feed(svc):
    now = datetime.now(UTC).isoformat()
    lines = []
    for host, svc_name, env in (("ea-app-01", "ea-portal", "prod"), ("ea-db-01", "ea-db", "prod"), ("hr-app-01", "hr-api", "prod"), ("fin-01", "ledger", "prod")):
        for i in range(5):
            lines.append({"ts": now, "level": "error" if i == 0 else "info", "service": svc_name, "host": host, "env": env, "msg": f"GET /api/x/{i} 200 {20 + i}ms",
                          "trace_id": f"{host}-{i}", "duration_ms": 20 + i})
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode(), "agent")
    svc.inventory.upsert({"hostname": "hr-app-01", "ip": "10.30.0.5", "application": "HR Bordro", "env": "prod"})
    svc.inventory.upsert({"hostname": "fin-01", "ip": "10.40.0.9", "application": "Finance", "env": "prod"})
    from watchover.live import simulate_flows
    svc.live.ingest("flows.jsonl", (json.dumps({"ts": now, "host": "ea-app-01", "msg": "flow out java 10.30.0.5:443", "kind": "flow", "flow.dir": "out", "flow.process": "java",
                                                "flow.remote": "10.30.0.5:443", "flow.count": 3}) + "\n").encode(), "agent")
    for host in ("ea-app-01", "hr-app-01", "fin-01"):
        svc.anomalies._upsert("errors", f"errors:prod:{host}", env="prod", host=host, title=f"{host} storm", detail="d", observed=40, baseline=5, spread=1, score=9, series=[])


def test_team_scoping_everywhere(app):
    c, svc = app, app.svc
    feed(svc)
    admin = login(c, "admin@watchover.local", "Watchover!Admin2026")
    bad = c.post("/api/teams", json={"name": "x", "rules": [{"field": "nope", "op": "glob", "value": "a"}]}, headers=admin)
    assert bad.status_code in (400, 422, 500) and "field" in bad.text
    pv = c.post("/api/teams/preview", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}]}, headers=admin).json()
    assert pv["hosts"] == ["ea-app-01", "ea-db-01"]
    ea = c.post("/api/teams", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}], "members": ["ea@corp.com"]}, headers=admin).json()
    hr = c.post("/api/teams", json={"name": "İK", "rules": [{"field": "application", "op": "glob", "value": "HR*"}, {"field": "service", "op": "glob", "value": "ik-*"}],
                                    "hosts": ["fin-01"], "members": ["hr@corp.com"]}, headers=admin).json()
    assert ea["matched"] == ["ea-app-01", "ea-db-01"] and hr["matched"] == ["fin-01", "hr-app-01"]
    h_ea, h_hr, h_free = login(c, "ea@corp.com"), login(c, "hr@corp.com"), login(c, "free@corp.com")
    hosts = lambda h: {e["host"] for e in c.get("/api/live/events?n=500", headers=h).json()}   # noqa: E731
    assert hosts(h_ea) == {"ea-app-01", "ea-db-01"} and hosts(h_hr) == {"hr-app-01", "fin-01"}
    assert hosts(h_free) == hosts(admin) == {"ea-app-01", "ea-db-01", "hr-app-01", "fin-01"}          # no team: everything (not strict)
    assert c.get("/api/live/events?n=50&host=hr-app-01", headers=h_ea).json() == []                    # asking for another team's host: nothing
    assert set(c.get("/api/live/hosts", headers=h_ea).json()["hosts"]) == {"ea-app-01", "ea-db-01"}
    assert c.get("/api/live/stats?window=60", headers=h_ea).json()["total"] == 10
    # anomalies: list, stats and single rows
    an = c.get("/api/anomalies?status=all", headers=h_ea).json()
    assert {a["host"] for a in an} == {"ea-app-01"} and c.get("/api/anomalies/stats", headers=h_ea).json()["open"] == 1
    other = next(a for a in c.get("/api/anomalies?status=all", headers=admin).json() if a["host"] == "fin-01")
    assert c.get(f"/api/anomalies/{other['id']}", headers=h_ea).status_code == 404
    assert c.post(f"/api/anomalies/{other['id']}/status", json={"status": "resolved"}, headers=h_ea).status_code == 404
    # APM: services by the hosts they ran on, traces, topology
    svcs = {s["service"] for s in c.get("/api/apm/services", headers=h_ea).json()["services"]}
    assert svcs == {"ea-portal", "ea-db"}
    assert c.get("/api/apm/services/ledger", headers=h_ea).status_code == 404 and c.get("/api/apm/services/ledger", headers=h_hr).status_code == 200
    tr = c.get("/api/traces?limit=100", headers=h_ea).json()
    assert tr and all(set(t["services"]) <= {"ea-portal", "ea-db"} for t in tr)
    assert c.get("/api/traces/fin-01-0", headers=h_ea).status_code == 404 and c.get("/api/traces/fin-01-0", headers=h_hr).status_code == 200
    # integrations: own hosts only; inventory and agents too
    assert {h["host"] for h in c.get("/api/apm/hosts", headers=h_ea).json()} == {"ea-app-01"}
    assert c.get("/api/apm/hosts/hr-app-01/integrations", headers=h_ea).status_code == 404
    assert {r["hostname"] for r in c.get("/api/inventory", headers=h_hr).json()} == {"hr-app-01", "fin-01"}
    assert c.get("/api/inventory", headers=h_ea).json() == []
    # user experience: apps by service rule
    svc.rum.create_app("ik-mobil", "android"); svc.rum.create_app("ea-mobile", "ios")
    assert [a["name"] for a in c.get("/api/rum/apps", headers=h_hr).json()["apps"]] == ["ik-mobil"]
    assert c.get("/api/rum/apps", headers=h_ea).json()["apps"] == []
    # who manages teams, and what a user is told
    assert c.get("/api/teams", headers=h_ea).status_code == 403
    assert c.get("/api/teams/mine", headers=h_ea).json() == {"all": False, "teams": ["EA"], "hosts": 2}
    assert c.get("/api/teams/mine", headers=admin).json()["all"] is True
    # scope.all on a role lifts the team limit; teams_strict closes the no-team door
    c.put(f"/api/teams/{ea['id']}", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}], "members": ["ea@corp.com", "noc@corp.com"]}, headers=admin)
    h_noc = login(c, "noc@corp.com")
    assert hosts(h_noc) == {"ea-app-01", "ea-db-01"}
    svc.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('noc', 'NOC', '', ?, 0)", (json.dumps(["page.ops", "scope.all"]),))
    svc.kb._exec("UPDATE users SET role='noc' WHERE email='noc@corp.com'"); svc.teams.invalidate()
    assert hosts(login(c, "noc@corp.com")) == hosts(admin)
    from watchover import settings
    cfg = settings.load(); cfg["teams_strict"] = True; settings.save(cfg); svc.teams.invalidate()
    assert hosts(login(c, "free@corp.com")) == set()
    assert c.delete(f"/api/teams/{hr['id']}", headers=admin).status_code == 204
