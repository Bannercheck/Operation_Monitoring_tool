"""Role-based teams (docs/design/team-roles.md) and team-only data (docs/design/team-scope-audit.md): role templates on team
creation, the effective-permission matrix (installation role ∪ team roles, a team role only on that team's hosts), team manager
limits, team-owned inventory / agents / thresholds / alerts / datasets / actions, every audited endpoint returning only the
caller's team's data, the migration of existing teams and the access cache."""
import io
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

UTC = timezone.utc
ADMIN = ("admin@watchover.local", "Watchover!Admin2026")
PW = "Str0ng!Passw0rd"


def _env(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def app(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email, role in (("ayse@corp.com", "operator"), ("mgr@corp.com", "operator"), ("op@corp.com", "operator"), ("free@corp.com", "viewer"),
                        ("dis@corp.com", "viewer"), ("new@corp.com", "viewer")):
        svc.users.register(email, PW, name=email.split("@")[0].title(), role=role)
    svc.users.set_status(svc.users.get("dis@corp.com")["id"], "disabled")
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def feed(svc):
    now = datetime.now(UTC).isoformat()
    lines = []
    for host, svc_name in (("ea-app-01", "ea-portal"), ("ea-db-01", "ea-db"), ("hr-app-01", "hr-api"), ("fin-01", "ledger"), ("un-01", "lonely")):
        for i in range(5):
            lines.append({"ts": now, "level": "error" if i == 0 else "info", "service": svc_name, "host": host, "env": "prod", "msg": f"GET /api/x/{i} 200 {20 + i}ms",
                          "trace_id": f"{host}-{i}", "duration_ms": 20 + i})
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode(), "agent")
    svc.inventory.upsert({"hostname": "hr-app-01", "ip": "10.30.0.5", "application": "HR Bordro", "env": "prod"})
    svc.inventory.upsert({"hostname": "fin-01", "ip": "10.40.0.9", "application": "Finance", "env": "prod"})
    svc.inventory.upsert({"hostname": "ea-app-01", "ip": "10.20.0.5", "application": "EA Portal", "env": "prod"})
    out = {}
    for host in ("ea-app-01", "hr-app-01", "fin-01"):
        out[host] = svc.anomalies._upsert("errors", f"errors:prod:{host}", env="prod", host=host, title=f"{host} storm", detail="d", observed=40, baseline=5, spread=1, score=9, series=[])["id"]
    svc.teams.invalidate()
    return out


def two_teams(c, admin):
    """EA (ea-*) and İK (application HR*, explicit fin-01 and fin-02). ayse: EA Operatör + İK İzleyici, mgr: EA manager, op: İK Operatör."""
    ea = c.post("/api/teams", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}]}, headers=admin).json()
    ik = c.post("/api/teams", json={"name": "İK", "rules": [{"field": "application", "op": "glob", "value": "HR*"}], "hosts": ["fin-01", "fin-02"]}, headers=admin).json()
    rid = lambda t, key: next(r["id"] for r in t["roles"] if r["key"] == key)   # noqa: E731
    for t, email, key in ((ea, "ayse@corp.com", "operator"), (ik, "ayse@corp.com", "viewer"), (ea, "mgr@corp.com", "manager"), (ik, "op@corp.com", "operator")):
        r = c.put(f"/api/teams/{t['id']}/members/{email}", json={"role_id": rid(t, key)}, headers=admin)
        assert r.status_code == 200, r.text
    uid = lambda e: c.svc.users.get(e)["id"]   # noqa: E731
    for e in ("ayse@corp.com", "mgr@corp.com"):                     # their installation role gives nothing: team roles decide
        assert c.patch(f"/api/users/{uid(e)}", json={"role": "member"}, headers=admin).status_code == 200
    return c.get(f"/api/teams/{ea['id']}", headers=admin).json(), c.get(f"/api/teams/{ik['id']}", headers=admin).json(), rid


# ---------------------------------------------------------------- 1. templates
def test_templates_on_creation_and_copy(app):
    from watchover import rbac
    c = app; admin = login(c, *ADMIN)
    names0 = {r["name"] for r in c.get("/api/roles", headers=admin).json()["roles"]}
    assert "member" not in names0                                    # created with the first team only
    t = c.post("/api/teams", json={"name": "EA", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}]}, headers=admin)
    assert t.status_code == 201
    t = t.json()
    assert [r["key"] for r in t["roles"]] == ["manager", "operator", "viewer"]
    for r in t["roles"]:
        assert set(r["perms"]) == rbac.TEAM_TEMPLATES[r["key"]]["perms"] and r["template"] == r["key"]
    logs = getattr(rbac, "LOG_PERMS", frozenset()) | getattr(rbac, "AUTO_PERMS", frozenset())   # the log explorer and the action agent add their own keys
    assert [len(rbac.TEAM_TEMPLATES[k]["perms"] - logs) for k in ("manager", "operator", "viewer")] == [25, 22, 13]
    assert t["default_role_id"] == next(r["id"] for r in t["roles"] if r["key"] == "viewer")
    assert "member" in {r["name"] for r in c.get("/api/roles", headers=admin).json()["roles"]}
    custom = c.post(f"/api/teams/{t['id']}/roles", json={"label": "Nöbetçi DBA", "perms": ["act.inventory"]}, headers=admin).json()
    assert custom["key"] == "nobetci_dba" and set(custom["perms"]) == {"act.inventory", "page.inv"}
    cp = c.post("/api/teams", json={"name": "EA-2", "copy_roles_from": t["id"]}, headers=admin).json()
    assert [(r["key"], sorted(r["perms"])) for r in cp["roles"]] == [(r["key"], sorted(r["perms"])) for r in t["roles"] + [custom]]
    cat = c.get("/api/teams/permissions", headers=login(c, "free@corp.com")).json()
    assert [s["id"] for s in cat["sections"]] == ["home", "apm", "signals", "respond", "intel", "security", "settings"]
    assert next(i for s in cat["sections"] for i in s["items"] if i["key"] == "act.inventory")["requires"] == "page.inv"


# ---------------------------------------------------------------- 2. normalize
def test_normalize_team_and_installation_roles(app):
    c = app; admin = login(c, *ADMIN)
    t = c.post("/api/teams", json={"name": "EA"}, headers=admin).json()
    rid = t["roles"][2]["id"]
    r = c.put(f"/api/teams/{t['id']}/roles/{rid}", json={"label": "İzleyici", "perms": ["sys.update", "scope.all", "page.llm", "bogus"]}, headers=admin).json()
    assert r["perms"] == []
    r = c.put(f"/api/teams/{t['id']}/roles/{rid}", json={"label": "İzleyici", "perms": ["act.inventory"]}, headers=admin).json()
    assert set(r["perms"]) == {"act.inventory", "page.inv"}
    c.put("/api/roles/lead", json={"perms": ["page.ops", "team.manage", "act.anomaly"], "label": "Lead"}, headers=admin)
    lead = next(x for x in c.get("/api/roles", headers=admin).json()["roles"] if x["name"] == "lead")
    assert set(lead["perms"]) == {"page.ops", "act.anomaly", "page.anom"}            # team.manage never in an installation role
    assert "sections" in c.get("/api/roles", headers=admin).json()


# ---------------------------------------------------------------- 3. effective permissions
def test_effective_permissions_matrix(app):
    from watchover import rbac
    c = app; svc = c.svc; an = feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    h = login(c, "ayse@corp.com")
    me = c.get("/api/auth/me", headers=h).json()
    want = rbac.MEMBER_ROLE["perms"] | rbac.TEAM_TEMPLATES["operator"]["perms"] | rbac.TEAM_TEMPLATES["viewer"]["perms"]
    assert set(me["permissions"]) == want and me["all"] is False and me["global_permissions"] == ["page.readme"]
    assert {(t["name"], t["role_label"]) for t in me["teams"]} == {("EA", "Operatör"), ("İK", "İzleyici")}
    assert c.put("/api/inventory", json={"hostname": "hr-app-01", "application": "HR Bordro", "note": "x"}, headers=h).status_code == 403
    assert c.put("/api/inventory", json={"hostname": "ea-app-01", "application": "EA Portal", "note": "x"}, headers=h).status_code == 201
    svcs = {s["service"] for s in c.get("/api/apm/services", headers=h).json()["services"]}
    assert svcs == {"ea-portal", "ea-db", "hr-api", "ledger"}
    assert c.post(f"/api/anomalies/{an['hr-app-01']}/status", json={"status": "ack"}, headers=h).status_code == 404
    assert c.post(f"/api/anomalies/{an['ea-app-01']}/status", json={"status": "ack"}, headers=h).status_code == 200
    assert c.get(f"/api/anomalies/{an['hr-app-01']}", headers=h).status_code == 200            # page.anom holds in İK too
    c.patch(f"/api/users/{svc.users.get('ayse@corp.com')['id']}", json={"role": "operator"}, headers=admin)
    h = login(c, "ayse@corp.com")
    assert c.post(f"/api/anomalies/{an['hr-app-01']}/status", json={"status": "ack"}, headers=h).status_code == 200   # G applies in all teams
    # a team role without a view keeps its pages away (nav and API alike)
    assert c.get("/api/scan/results", headers=login(c, "free@corp.com")).status_code == 403


# ---------------------------------------------------------------- 4. no team / strict / admin / scope.all
def test_no_team_strict_admin_scope_all(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    two_teams(c, admin)
    hosts = lambda h: {e["host"] for e in c.get("/api/live/events?n=500", headers=h).json()}   # noqa: E731
    everything = {"ea-app-01", "ea-db-01", "hr-app-01", "fin-01", "un-01"}
    assert hosts(admin) == everything and hosts(login(c, "free@corp.com")) == everything
    assert hosts(login(c, "op@corp.com")) == {"hr-app-01", "fin-01"}
    assert c.get("/api/auth/me", headers=login(c, "free@corp.com")).json()["all"] is True
    svc.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('noc', 'NOC', '', ?, 0)", (json.dumps(["page.ops", "scope.all"]),))
    svc.kb._exec("UPDATE users SET role='noc' WHERE email='op@corp.com'"); svc.teams.invalidate()
    assert hosts(login(c, "op@corp.com")) == everything
    from watchover import settings
    cfg = settings.load(); cfg["teams_strict"] = True; settings.save(cfg)
    assert hosts(login(c, "free@corp.com")) == set()


# ---------------------------------------------------------------- 5. team manager limits
def test_team_manager_limits(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, rid = two_teams(c, admin)
    m = login(c, "mgr@corp.com")
    assert [t["name"] for t in c.get("/api/teams", headers=m).json()["teams"]] == ["EA"]
    op_role = rid(ea, "operator")
    r = c.put(f"/api/teams/{ea['id']}/roles/{op_role}", json={"label": "Operatör", "perms": ["page.ops", "page.inv", "act.inventory"]}, headers=m)
    assert r.status_code == 200 and set(r.json()["perms"]) == {"page.ops", "page.inv", "act.inventory"}
    # narrow the manager role (admin), then the manager may not grant what it lost
    c.put(f"/api/teams/{ea['id']}/roles/{rid(ea, 'manager')}", json={"label": "Ekip yöneticisi", "perms": ["page.ops", "page.inv", "act.inventory", "team.manage"]}, headers=admin)
    m = login(c, "mgr@corp.com")
    assert c.post(f"/api/teams/{ea['id']}/roles", json={"label": "Tarama", "perms": ["act.scan"]}, headers=m).status_code == 403
    assert c.post(f"/api/teams/{ea['id']}/roles", json={"label": "Yarı", "perms": ["team.manage"]}, headers=m).status_code == 403
    assert c.put(f"/api/teams/{ea['id']}/roles/{rid(ea, 'manager')}", json={"label": "x", "perms": ["page.ops"]}, headers=m).status_code == 403
    assert c.put(f"/api/teams/{ea['id']}/members/ayse@corp.com", json={"role_id": rid(ea, "manager")}, headers=m).status_code == 403
    assert c.put(f"/api/teams/{ea['id']}/members/mgr@corp.com", json={"role_id": rid(ea, "viewer")}, headers=m).status_code == 403
    assert c.put(f"/api/teams/{ik['id']}/members/new@corp.com", json={}, headers=m).status_code in (403, 404)
    assert c.get(f"/api/teams/{ik['id']}", headers=m).status_code in (403, 404)
    for path in ("/api/teams/known-hosts", "/api/teams/unowned"):
        assert c.get(path, headers=m).status_code == 403
    assert c.put(f"/api/teams/{ea['id']}", json={"name": "EA", "rules": []}, headers=m).status_code == 403
    assert c.get("/api/users", headers=m).status_code == 403                               # no account list for a team manager (KVKK)
    small = c.post(f"/api/teams/{ea['id']}/roles", json={"label": "Envanter", "perms": ["page.inv"]}, headers=admin).json()
    wide = c.put(f"/api/teams/{ea['id']}/members/new@corp.com", json={"role_id": small["id"]}, headers=m)
    assert wide.status_code == 403 and "every host" in wide.text                          # new@ is a viewer in no team: joining narrows it
    svc.users.set_role(svc.users.get("new@corp.com")["id"], "member", svc.roles.names())
    assert c.put(f"/api/teams/{ea['id']}/members/new@corp.com", json={"role_id": rid(ea, "viewer")}, headers=m).status_code == 403   # beyond the narrowed manager
    ok = c.put(f"/api/teams/{ea['id']}/members/new@corp.com", json={"role_id": small["id"]}, headers=m)
    assert ok.status_code == 200 and ok.json()["role_label"] == "Envanter"
    a = c.put(f"/api/teams/{ea['id']}/members/nobody@corp.com", json={}, headers=m)
    b = c.put(f"/api/teams/{ea['id']}/members/dis@corp.com", json={}, headers=m)
    assert a.status_code == b.status_code == 404 and a.json() == b.json()
    view = c.get(f"/api/teams/{ea['id']}/members", headers=login(c, "ayse@corp.com")).json()
    assert view and all("email" not in x for x in view)
    assert all("email" in x for x in c.get(f"/api/teams/{ea['id']}/members", headers=m).json())
    ev = [e for e in svc.users.events(50) if e["event"] == "team"]
    assert any("+member" in e["detail"] for e in ev) and not any("@" in e["detail"] for e in ev)


# ---------------------------------------------------------------- 6. host claims (team-owned inventory)
def test_host_claims_and_inventory_boundaries(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    a, o = login(c, "ayse@corp.com"), login(c, "op@corp.com")
    r = c.put("/api/inventory", json={"hostname": "ea-new-01", "team_id": ea["id"]}, headers=a)
    assert r.status_code == 201, r.text
    meta = next(h for h in c.get(f"/api/teams/{ea['id']}", headers=admin).json()["host_meta"] if h["host"] == "ea-new-01")
    assert meta["origin"] == "inventory" and meta["added_by"] == "Ayse"
    assert c.get("/api/inventory/ea-new-01", headers=a).status_code == 200 and c.get("/api/inventory/ea-new-01", headers=o).status_code == 404
    r = c.put("/api/inventory", json={"hostname": "gh-new-01"}, headers=a)                   # ayse holds act.inventory only in EA: picked
    assert r.status_code == 201 and "gh-new-01" in c.get(f"/api/teams/{ea['id']}", headers=admin).json()["hosts"]
    r = c.put("/api/inventory", json={"hostname": "fin-02", "team_id": ea["id"]}, headers=a)  # İK's explicit host
    assert r.status_code == 409 and "another team" in r.text and "İK" not in r.text
    r = c.put("/api/inventory", json={"hostname": "un-01", "team_id": ea["id"]}, headers=a)   # known, no team
    assert r.status_code == 403 and "unowned" in r.text
    from watchover import settings
    cfg = settings.load(); cfg["teams_self_claim"] = True; settings.save(cfg)
    assert c.put("/api/inventory", json={"hostname": "un-01", "team_id": ea["id"]}, headers=a).status_code == 201
    mv = c.post(f"/api/teams/{ea['id']}/hosts", json={"hosts": ["fin-02"], "move": True}, headers=admin).json()
    assert mv["added"] == ["fin-02"] and "fin-02" not in c.get(f"/api/teams/{ik['id']}", headers=admin).json()["hosts"]
    m = login(c, "mgr@corp.com")
    rej = c.post(f"/api/teams/{ea['id']}/hosts", json={"hosts": ["fin-01", "brand-new-9"]}, headers=m).json()
    assert rej["added"] == ["brand-new-9"] and rej["rejected"] == [{"host": "fin-01", "reason": "other_team"}]
    # stats / export / import scoped
    assert "ea-app-01" not in c.get("/api/inventory/export", headers=o).text and "hr-app-01" in c.get("/api/inventory/export", headers=o).text
    assert c.get("/api/inventory/stats", headers=o).json()["hosts"] == 2
    csv = "hostname,application\nhr-app-01,HR Bordro\nea-app-01,HACK\nik-new-01,HR Yeni\n"
    imp = c.post("/api/inventory/import", files={"file": ("i.csv", csv.encode(), "text/csv")}, headers=o).json()
    assert imp["imported"] == 2 and len(imp["errors"]) == 1 and "ea-app-01" in imp["errors"][0]
    assert svc.inventory.get("ea-app-01")["application"] == "EA Portal"
    assert c.delete("/api/inventory/ea-app-01", headers=o).status_code == 404 and c.delete("/api/inventory/hr-app-01", headers=o).status_code == 200


# ---------------------------------------------------------------- 7. agents
def test_agents_team_key_and_boundaries(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    a = login(c, "ayse@corp.com")
    key = c.post(f"/api/teams/{ea['id']}/enroll-key/rotate", headers=a).json()["key"]
    rec, tok = svc.agents.self_enroll(key, "ea-agent-07", "prod")
    assert rec["team_id"] == ea["id"] and tok.startswith("wo_")
    assert next(h for h in svc.teams.get(ea["id"])["host_meta"] if h["host"] == "ea-agent-07")["origin"] == "agent"
    with pytest.raises(ValueError):
        svc.agents.self_enroll(key, "hr-app-01")
    with pytest.raises(PermissionError):
        svc.agents.self_enroll("wk_wrong", "ea-x")
    for path in ("/api/agents/enroll-key",):
        assert c.get(path, headers=a).status_code == 403
    assert c.post("/api/agents/enroll-key/rotate", headers=a).status_code == 403
    other, _ = svc.agents.enroll("hr-app-01", "prod")
    assert c.get(f"/api/agents/{other['id']}", headers=a).status_code == 404       # İK İzleyici has no page.conn: not even visible
    assert c.get(f"/api/agents/{other['id']}", headers=login(c, "op@corp.com")).status_code == 200
    o_ids = {x["name"] for x in c.get("/api/agents", headers=login(c, "op@corp.com")).json()}
    assert o_ids == {"hr-app-01"}
    assert c.post(f"/api/agents/{other['id']}/revoke", headers=a).status_code == 404
    assert c.post(f"/api/agents/{rec['id']}/rotate", headers=login(c, "op@corp.com")).status_code == 404
    new = c.post("/api/agents", json={"name": "ea-agent-08"}, headers=a)
    assert new.status_code == 201 and new.json()["agent"]["team_id"] == ea["id"]
    assert c.post("/api/agents", json={"name": "fin-01"}, headers=a).status_code == 409


# ---------------------------------------------------------------- 8. thresholds
def test_team_threshold_policies(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    m, a = login(c, "mgr@corp.com"), login(c, "ayse@corp.com")
    assert c.post("/api/notify/recipients", json={"name": "ea-nobet", "email": "nobet@ea.corp", "groups": "ea-dba"}, headers=m).status_code == 201
    inst = c.post("/api/thresholds", json={"scope": "host", "scope_value": "ea-app-01", "metric": "cpu", "warn": 70, "crit": 90}, headers=admin)
    assert inst.status_code == 201
    assert c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50}, headers=a).status_code == 403
    bad = c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "channels": "webhook"}, headers=m)
    assert bad.status_code == 400 and "webhook" in bad.text
    assert c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "targets": "boss@evil.com"}, headers=m).status_code == 400
    assert c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "targets": "noc"}, headers=m).status_code == 400
    p = c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "crit": 60, "targets": "ea-nobet"}, headers=m)
    assert p.status_code == 201 and p.json()["team_id"] == ea["id"]
    eff = svc.thresholds.effective("ea-app-01", "prod")
    assert eff["cpu"]["id"] == p.json()["id"]                                     # team "all" beats installation "host"
    assert svc.thresholds.effective("hr-app-01", "prod").get("cpu", {}).get("id") != p.json()["id"]
    rows = c.get("/api/thresholds", headers=c.get and login(c, "op@corp.com")).json()
    assert [r["id"] for r in rows] == [inst.json()["id"]] and rows[0]["editable"] is False
    assert c.put(f"/api/thresholds/{p.json()['id']}", json={"scope": "all", "metric": "cpu", "warn": 40}, headers=login(c, "op@corp.com")).status_code == 404
    assert c.delete(f"/api/thresholds/{inst.json()['id']}", headers=m).status_code == 403
    pv = c.post("/api/thresholds/preview", json={"scope": "all", "metric": "cpu", "warn": 1, "team_id": ea["id"]}, headers=m).json()
    assert set(pv["hosts"]) <= {"ea-app-01", "ea-db-01"}


# ---------------------------------------------------------------- 9. notify
def test_team_alerts(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    m = login(c, "mgr@corp.com")
    assert c.post("/api/notify/recipients", json={"name": "İK nöbet", "phone": "+905551112233", "team_id": ik["id"]}, headers=admin).status_code == 201
    assert c.post("/api/notify/recipients", json={"name": "ea-nobet", "email": "nobet@ea.corp"}, headers=m).status_code == 201
    assert c.post("/api/notify/groups", json={"name": "ea-dba"}, headers=m).status_code == 201
    rows = c.get("/api/notify/recipients", headers=m).json()
    assert [r["name"] for r in rows] == ["ea-nobet"] and "+90555" not in json.dumps(rows)
    ik_rid = next(r["id"] for r in c.get("/api/notify/recipients", headers=admin).json() if r["name"] == "İK nöbet")
    assert c.patch(f"/api/notify/recipients/{ik_rid}", json={"email": "x@evil.com"}, headers=m).status_code == 404
    assert c.delete(f"/api/notify/recipients/{ik_rid}", headers=m).status_code == 404
    rule = c.post("/api/notify/rules", json={"name": "EA hatalar", "condition": "errors", "threshold": 0.1, "targets": "ea-nobet", "cooldown_min": 1}, headers=m)
    assert rule.status_code == 201
    assert c.post("/api/notify/rules", json={"name": "x", "condition": "errors", "channels": "webhook", "targets": "ea-nobet"}, headers=m).status_code == 400
    svc.alerts.evaluate()
    got = [x for x in svc.notifier.alerts(20) if x.get("team_id") == ea["id"]]
    assert got and "2 ERROR+ events" in got[0]["body"]                        # only EA's two hosts were counted
    assert [x["id"] for x in c.get("/api/notify/alerts", headers=m).json()] == [g["id"] for g in got]
    assert c.get("/api/notify/recipients", headers=login(c, "op@corp.com")).status_code == 403
    assert c.post("/api/notify/test", json={"channel": "email", "target": "a@b.c"}, headers=m).status_code == 403


# ---------------------------------------------------------------- 10. datasets and actions
def _upload(c, h, name, lines, team_id=None):
    data = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    r = c.post("/api/datasets", files=[("files", (name, data, "application/json"))], data=({"team_id": str(team_id)} if team_id else {}), headers=h)
    assert r.status_code == 202, r.text
    jid = r.json()["jobs"][0]["id"]
    for _ in range(200):
        j = c.get(f"/api/datasets/jobs/{jid}", headers=h).json()
        if j["state"] != "running":
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    return j["dataset"]


def _lines(host, marker):
    now = datetime.now(UTC)
    out = []
    for i in range(60):
        out.append({"ts": now.isoformat(), "level": "error" if i % 3 == 0 else "info", "service": f"{host}-svc", "host": host, "env": "prod",
                    "msg": f"{marker} payment db timeout after {100 + i} ms on {host}" if i % 3 == 0 else f"GET /ok {i}"})
    return out


def test_team_datasets_and_actions(app):
    c = app; svc = c.svc; an = feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    a, o = login(c, "ayse@corp.com"), login(c, "op@corp.com")
    r = c.post("/api/datasets", files=[("files", ("x.jsonl", b'{"msg": "x"}\n', "application/json"))], headers=a)
    assert r.status_code == 400 and "team_id" in r.text                      # page.data in two teams: say which
    key = _upload(c, a, "ea.jsonl", _lines("ea-app-01", "EASECRET"), ea["id"])
    assert [d["id"] for d in c.get("/api/datasets", headers=a).json()] == [key]
    assert c.get("/api/datasets", headers=o).json() == []
    for path in (f"/api/datasets/{key}", f"/api/datasets/{key}/signals", f"/api/datasets/{key}/search?q=EASECRET", f"/api/apm/services?dataset={key}",
                 f"/api/map?dataset={key}"):
        assert c.get(path, headers=o).status_code == 404, path
    assert c.delete(f"/api/datasets/{key}", headers=o).status_code == 404 and key in svc.datasets.items
    act = c.post(f"/api/anomalies/{an['ea-app-01']}/action", headers=a).json()["action"]
    assert act["host"] == "ea-app-01"
    assert c.get(f"/api/actions/{act['id']}", headers=o).status_code == 404 and c.get(f"/api/actions/{act['id']}", headers=a).status_code == 200
    assert act["id"] not in {x["id"] for x in c.get("/api/actions", headers=o).json()}
    mine = c.post("/api/actions", json={"incident_id": "X-1", "title": "EA iş", "team_id": ea["id"]}, headers=a).json()
    assert mine["team_id"] == ea["id"] and c.patch(f"/api/actions/{mine['id']}", json={"title": "HIJACK"}, headers=o).status_code == 404


# ---------------------------------------------------------------- the audit: every endpoint, Team A never sees or changes Team B
def test_audit_endpoints_team_only(app):
    c = app; svc = c.svc; admin = login(c, *ADMIN)
    now = datetime.now(UTC).isoformat()
    lines = [{"ts": now, "level": "error", "service": s, "host": h, "env": "prod", "msg": f"{m} failure {i}"} for h, s, m in
             (("a-web-01", "a-web", "ASAFE"), ("b-web-01", "b-web", "BSECRET"), ("b-db-01", "b-db", "BSECRET")) for i in range(4)]
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode(), "agent")
    for h, ip in (("a-web-01", "10.1.0.1"), ("b-web-01", "10.2.0.1"), ("b-db-01", "10.2.0.2")):
        svc.inventory.upsert({"hostname": h, "ip": ip, "application": h[:1].upper() + "-App", "note": "BSECRET" if h.startswith("b") else ""})
    svc.teams.invalidate()
    c.put("/api/roles/team_power", json={"perms": sorted(set(svc.roles.perms("operator")) | {"sys.notify"}), "label": "Team power"}, headers=admin)
    for e in ("alice@corp.com", "bob@corp.com"):
        svc.users.set_role(svc.users.register(e, PW)["id"], "team_power", svc.roles.names())
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}], "members": ["alice@corp.com"]}, headers=admin).json()
    tb = c.post("/api/teams", json={"name": "B", "rules": [{"field": "host", "op": "glob", "value": "b-*"}], "members": ["bob@corp.com"]}, headers=admin).json()
    mgr_b = next(r["id"] for r in tb["roles"] if r["key"] == "manager")
    assert c.put(f"/api/teams/{tb['id']}/members/bob@corp.com", json={"role_id": mgr_b}, headers=admin).status_code == 200
    al, bo = login(c, "alice@corp.com"), login(c, "bob@corp.com")
    # Team B's objects, created by bob (Team B's manager)
    bkey = _upload(c, bo, "bob-billing.jsonl", _lines("b-db-01", "BSECRET"))
    note = c.post("/api/knowledge/notes", json={"title": "BSECRET runbook", "text": "password hint BSECRET"}, headers=bo).json()
    brule = c.post("/api/knowledge/rules", json={"kind": "noise_template", "key": "BSECRET x", "value": "1"}, headers=bo).json()["id"]
    rcp = c.post("/api/notify/recipients", json={"name": "bob", "email": "bob@corp.com", "phone": "+905551234567"}, headers=bo).json()["id"]
    nrule = c.post("/api/notify/rules", json={"name": "B rule", "condition": "errors", "targets": "bob"}, headers=bo).json()["id"]
    grp = c.post("/api/notify/groups", json={"name": "b-team"}, headers=bo).json()["id"]
    pol = c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "targets": "bob"}, headers=bo)
    assert pol.status_code == 201 and pol.json()["team_id"] == tb["id"], pol.text
    pol = pol.json()["id"]
    bact = c.post("/api/actions", json={"incident_id": "B-1", "title": "BSECRET rotate billing db creds"}, headers=bo).json()["id"]
    flow = c.post("/api/flows", json={"name": "B order flow", "key_regex": "order=(ORD-B\\d+)", "steps": [{"name": "a", "match": "created"}, {"name": "b", "match": "paid"}]}, headers=bo)
    assert flow.status_code == 201, flow.text
    flow = flow.json()["id"]
    src = c.post("/api/sources", json={"name": "B source", "kind": "http", "url": "http://b.example/logs"}, headers=bo)
    assert src.status_code == 201, src.text
    src = src.json()["id"]
    bagent, _ = svc.agents.enroll("b-db-01", "prod")
    svc.scanner.scan_host(svc.inventory.get("b-db-01"))
    an_b = svc.anomalies._upsert("errors", "errors:prod:b-db-01", env="prod", host="b-db-01", title="b storm", detail="d", observed=40, baseline=5, spread=1, score=9, series=[])
    svc.memory.remember_anomaly(an_b)
    leak = lambda r: "BSECRET" in r.text or "b-db-01" in r.text or "10.2.0" in r.text or "+90555" in r.text   # noqa: E731
    reads = ["/api/datasets", f"/api/datasets/{bkey}", f"/api/datasets/{bkey}/search?q=BSECRET", f"/api/datasets/{bkey}/export?fmt=csv", f"/api/datasets/{bkey}/signals",
             f"/api/datasets/{bkey}/incidents", "/api/knowledge/search?q=BSECRET", "/api/knowledge/lessons", "/api/knowledge/rules", "/api/memory/search?q=BSECRET",
             "/api/memory/search", "/api/notify/recipients", "/api/notify/groups", "/api/notify/rules", "/api/notify/alerts", "/api/scan/results",
             "/api/scan/results/b-db-01", "/api/scan/stats", "/api/inventory/export", "/api/inventory/b-db-01", "/api/inventory", "/api/grok/templates",
             "/api/actions", f"/api/actions/{bact}", "/api/playbook", "/api/flows", f"/api/flows/{flow}/funnel", f"/api/apm/services?dataset={bkey}",
             f"/api/map?dataset={bkey}", "/api/live/stats?window=60", f"/api/agents/{bagent['id']}", "/api/agents", "/api/sources", f"/api/sources/{src}",
             "/api/itsm/config", "/api/itsm/tickets", "/api/anomalies?status=all", "/api/memory/timeline", "/api/llm/training-export", "/api/thresholds",
             f"/api/playbook/entry?key=BSECRET", "/api/scan/runs"]
    for path in reads:
        r = c.get(path, headers=al)
        assert r.status_code in (200, 403, 404) and not (r.status_code == 200 and leak(r)), (path, r.status_code, r.text[:300])   # a 404 echoes the asked id
    filtered = ["/api/datasets", "/api/knowledge/search?q=BSECRET", "/api/knowledge/lessons", "/api/knowledge/rules", "/api/memory/search?q=BSECRET",
                "/api/notify/recipients", "/api/notify/rules", "/api/notify/alerts", "/api/scan/results", "/api/scan/stats", "/api/inventory/export", "/api/grok/templates",
                "/api/actions", "/api/playbook", "/api/flows", "/api/live/stats?window=60", "/api/agents", "/api/sources", "/api/itsm/config", "/api/thresholds"]
    assert [(p, c.get(p, headers=al).text[:200]) for p in filtered if c.get(p, headers=al).status_code != 200] == []   # answered, and filtered (not merely refused)
    assert c.post("/api/assist/ask", json={"question": "BSECRET nedir?"}, headers=al).status_code == 200
    assert not leak(c.post("/api/assist/ask", json={"question": "BSECRET nedir?"}, headers=al))
    assert c.post("/api/assist/ask", json={"question": "x", "dataset": bkey}, headers=al).status_code == 404
    writes = [("delete", f"/api/datasets/{bkey}", None), ("patch", f"/api/notify/recipients/{rcp}", {"email": "alice@corp.com"}),
              ("delete", f"/api/notify/recipients/{rcp}", None), ("patch", f"/api/notify/rules/{nrule}", {"targets": "alice@corp.com"}),
              ("delete", f"/api/notify/rules/{nrule}", None), ("delete", f"/api/notify/groups/{grp}", None), ("post", "/api/scan/host/b-db-01", {}),
              ("put", "/api/inventory", {"hostname": "b-db-01", "application": "A-OWNED-NOW"}), ("delete", "/api/inventory/b-web-01", None),
              ("patch", f"/api/actions/{bact}", {"title": "HIJACKED"}), ("delete", f"/api/actions/{bact}", None), ("delete", f"/api/flows/{flow}", None),
              ("put", f"/api/flows/{flow}", {"name": "x", "key_regex": "(x)", "steps": [{"name": "a", "match": "a"}, {"name": "b", "match": "b"}]}),
              ("patch", f"/api/sources/{src}", {"url": "http://attacker"}), ("delete", f"/api/sources/{src}", None), ("post", f"/api/sources/{src}/poll", None),
              ("post", f"/api/agents/{bagent['id']}/rotate", None), ("post", f"/api/agents/{bagent['id']}/revoke", None), ("delete", f"/api/agents/{bagent['id']}", None),
              ("post", "/api/agents/enroll-key/rotate", None), ("post", f"/api/knowledge/rules/{brule}/decide", {"approve": True}),
              ("delete", f"/api/knowledge/lessons/{note['id']}", None), ("put", "/api/itsm/config", {"base": "http://attacker"}),
              ("post", f"/api/anomalies/{an_b['id']}/status", {"status": "resolved"}), ("delete", f"/api/thresholds/{pol}", None),
              ("put", f"/api/thresholds/{pol}", {"scope": "all", "metric": "cpu", "warn": 99})]
    for verb, path, body in writes:
        r = getattr(c, verb)(path, headers=al, **({"json": body} if body is not None else {}))
        assert r.status_code in (403, 404), (verb, path, r.status_code, r.text[:200])
    assert c.post("/api/scan/all", json={}, headers=al).status_code in (200, 400)
    assert svc.inventory.get("b-db-01")["application"] == "B-App" and svc.inventory.get("b-web-01") is not None
    assert bkey in svc.datasets.items and svc.actions.get(bact)["title"].startswith("BSECRET") and svc.agents.get(bagent["id"])["status"] == "active"
    assert {x["name"] for x in c.get("/api/notify/recipients", headers=bo).json()} == {"bob"}
    # bob still sees and changes his own
    assert c.get(f"/api/datasets/{bkey}", headers=bo).status_code == 200 and c.get(f"/api/actions/{bact}", headers=bo).status_code == 200
    assert "BSECRET runbook" in c.get("/api/knowledge/search?q=BSECRET", headers=bo).text
    assert c.get("/api/teams", headers=al).status_code == 403 and c.get("/api/teams/known-hosts", headers=al).status_code == 403
    assert ta["id"] != tb["id"]


# ---------------------------------------------------------------- 11. migration
def _old_schema(path):
    """The tables as v1.45 left them: teams without roles, members without a team role, roles before the page split."""
    from watchover import rbac
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', rules TEXT DEFAULT '[]', created_at TEXT)")
    db.execute("CREATE TABLE team_hosts (team_id INTEGER NOT NULL, host TEXT NOT NULL, PRIMARY KEY (team_id, host))")
    db.execute("CREATE TABLE team_members (team_id INTEGER NOT NULL, email TEXT NOT NULL, PRIMARY KEY (team_id, email))")
    db.execute("CREATE TABLE roles (name TEXT PRIMARY KEY, label TEXT DEFAULT '', description TEXT DEFAULT '', perms TEXT DEFAULT '[]', builtin INTEGER DEFAULT 0)")
    old = lambda p: sorted(set(p) - rbac.SPLIT_PERMS - getattr(rbac, "LOG_PERMS", frozenset()))   # noqa: E731
    for name in ("admin", "operator", "viewer"):
        db.execute("INSERT INTO roles VALUES (?,?,?,?,1)", (name, name, "", json.dumps(old(rbac.BUILTIN[name]["perms"]))))
    db.execute("INSERT INTO roles VALUES ('auditor','Auditor','',?,0)", (json.dumps(["page.ops", "page.data"]),))
    db.execute("INSERT INTO teams (name, rules) VALUES ('EA', ?)", (json.dumps([{"field": "host", "op": "glob", "value": "ea-*"}]),))
    db.execute("INSERT INTO teams (name, rules) VALUES ('İK', ?)", (json.dumps([{"field": "application", "op": "glob", "value": "HR*"}]),))
    db.execute("INSERT INTO team_hosts VALUES (2, 'fin-01')")
    for tid, e in ((1, "ea@corp.com"), (2, "hr@corp.com"), (1, "boss@corp.com"), (2, "aud@corp.com")):
        db.execute("INSERT INTO team_members VALUES (?,?)", (tid, e))
    db.commit(); db.close()


def test_migration_keeps_access(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _old_schema(str(tmp_path / "k.db"))
    from watchover.auth import Users
    from watchover.knowledge import Knowledge
    kb = Knowledge(str(tmp_path / "k.db")); us = Users(kb); us.bootstrap()
    for e, role in (("ea@corp.com", "operator"), ("hr@corp.com", "viewer"), ("boss@corp.com", "admin"), ("aud@corp.com", "auditor")):
        u = us.register(e, PW); us.set_role(u["id"], role, ["admin", "operator", "viewer", "auditor"])
    kb._exec("UPDATE users SET must_change=0"); kb.close()
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    feed(svc)
    with TestClient(a) as c:
        admin = login(c, *ADMIN)
        teams = {t["name"]: t for t in c.get("/api/teams", headers=admin).json()["teams"]}
        assert [r["key"] for r in teams["EA"]["roles"]] == ["manager", "operator", "viewer"]
        role_of = {m["email"]: m["role_label"] for t in teams.values() for m in t["memberships"]}
        assert role_of == {"ea@corp.com": "Operatör", "boss@corp.com": "Ekip yöneticisi", "hr@corp.com": "İzleyici", "aud@corp.com": "İzleyici"}
        roles = {r["name"]: set(r["perms"]) for r in c.get("/api/roles", headers=admin).json()["roles"]}
        assert {"page.anom", "page.thr", "page.actions"} <= roles["auditor"] and {"page.anom", "page.thr", "page.actions"} <= roles["viewer"]
        assert "member" in roles and "page.anom" in roles["operator"]
        hosts = lambda h: {e["host"] for e in c.get("/api/live/events?n=500", headers=h).json()}   # noqa: E731
        assert hosts(login(c, "ea@corp.com")) == {"ea-app-01", "ea-db-01"} and hosts(login(c, "hr@corp.com")) == {"hr-app-01", "fin-01"}
        assert hosts(login(c, "aud@corp.com")) == {"hr-app-01", "fin-01"}
        assert c.get("/api/inventory", headers=login(c, "hr@corp.com")).status_code == 200            # the one gain: read-only inventory, own team
        assert c.get("/api/anomalies", headers=login(c, "aud@corp.com")).status_code == 200
        c.put("/api/roles/auditor", json={"perms": ["page.ops", "page.data", "page.thr", "page.actions"], "label": "Auditor"}, headers=admin)
    svc.close()
    b = create_app()                                                                                  # restart: the split does not come back
    assert "page.anom" not in b.state.services.roles.perms("auditor")
    b.state.services.close()


# ---------------------------------------------------------------- 12. delete
def test_delete_team_with_owned_records(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, _rid = two_teams(c, admin)
    m = login(c, "mgr@corp.com")
    c.post("/api/notify/recipients", json={"name": "ea-nobet", "email": "n@ea.corp"}, headers=m)
    pid = c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50, "targets": "ea-nobet"}, headers=m).json()["id"]
    r = c.delete(f"/api/teams/{ea['id']}", headers=admin)
    assert r.status_code == 409 and r.json()["owned"]["thresholds"] == 1 and r.json()["owned"]["recipients"] == 1
    assert c.delete(f"/api/teams/{ea['id']}?transfer=install", headers=admin).status_code == 204
    p = svc.thresholds.get(pid)
    assert p["team_id"] is None and p["enabled"] == 0
    assert c.delete(f"/api/teams/{ik['id']}", headers=admin).status_code == 204                 # only explicit hosts: nothing to transfer


# ---------------------------------------------------------------- 13. cache
def test_changes_apply_on_next_request(app):
    c = app; svc = c.svc; feed(svc); admin = login(c, *ADMIN)
    ea, ik, rid = two_teams(c, admin)
    a = login(c, "ayse@corp.com")
    assert "act.inventory" in c.get("/api/auth/me", headers=a).json()["permissions"]
    c.put(f"/api/teams/{ea['id']}/members/ayse@corp.com", json={"role_id": rid(ea, "viewer")}, headers=admin)
    assert "act.inventory" not in c.get("/api/auth/me", headers=a).json()["permissions"]
    c.put(f"/api/teams/{ea['id']}/roles/{rid(ea, 'viewer')}", json={"label": "İzleyici", "perms": ["page.ops", "act.scan"]}, headers=admin)
    me = c.get("/api/auth/me", headers=a).json()["permissions"]
    assert "act.scan" in me and "page.scan" in me and "page.apm" in me                          # İK İzleyici still gives page.apm
    assert c.get("/api/teams/mine", headers=a).json() == {"all": False, "teams": ["EA", "İK"], "hosts": 5}   # + İK's pre-registered fin-02
