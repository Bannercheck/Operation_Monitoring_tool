"""WP3: action-agent permissions (§10.9): auto_v1 grants nothing that executes, team scope on /api/act, can_approve, kill basics."""
import json

import pytest

from watchover import rbac
from watchover.access import Access, Membership
from watchover.db import Database

ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"
POWER = {"act.execute", "act.approve", "act.publish", "act.promote", "act.policy", "act.golden", "act.audit", "act.kill"}


def test_auto_v1_grants_nothing_that_executes(tmp_path):
    kb = Database(str(tmp_path / "k.db"))
    # an installation from before the action agent: stored roles (one customised), a team role, no auto_v1 marker
    kb._exec("CREATE TABLE roles (name TEXT PRIMARY KEY, label TEXT DEFAULT '', description TEXT DEFAULT '', perms TEXT DEFAULT '[]', builtin INTEGER DEFAULT 0)")
    kb._exec("CREATE TABLE rbac_meta (key TEXT PRIMARY KEY, value TEXT)")
    for k in ("split_v1", "logs_v1"):
        kb._exec("INSERT INTO rbac_meta (key, value) VALUES (?, '1')", (k,))
    old_op = sorted(rbac.BUILTIN["operator"]["perms"] - rbac.AUTO_PERMS)
    old_viewer = sorted(rbac.BUILTIN["viewer"]["perms"] - rbac.AUTO_PERMS)
    kb._exec("INSERT INTO roles VALUES ('admin','','','[]',1)")
    kb._exec("INSERT INTO roles VALUES ('operator','','',?,1)", (json.dumps(old_op),))
    kb._exec("INSERT INTO roles VALUES ('viewer','','',?,1)", (json.dumps(old_viewer),))
    kb._exec("INSERT INTO roles VALUES ('noc','','',?,0)", (json.dumps(["page.actions", "page.ops", "act.anomaly"]),))
    kb._exec("INSERT INTO roles VALUES ('auditor','','',?,0)", (json.dumps(["page.ops"]),))
    kb._exec("CREATE TABLE team_roles (id INTEGER PRIMARY KEY, team_id INTEGER, perms TEXT)")
    kb._exec("INSERT INTO team_roles (id, team_id, perms) VALUES (1, 1, ?)", (json.dumps(["page.actions", "act.alerts", "team.manage"]),))
    roles = rbac.Roles(kb)
    perms = {r["name"]: r["perms"] for r in roles.list()}
    assert {"page.auto", "act.case", "act.runbook"} <= perms["operator"] and not (perms["operator"] & POWER)
    assert "page.auto" in perms["viewer"] and not (perms["viewer"] & (POWER | {"act.case"}))
    assert perms["noc"] == {"page.actions", "page.ops", "act.anomaly", "page.auto", "page.apps"}     # + page.apps (apps_v1, view only)
    assert "page.auto" not in perms["auditor"]
    tr = set(json.loads(kb._exec("SELECT perms FROM team_roles WHERE id=1")[0]["perms"]))
    assert "page.auto" in tr and not (tr & POWER)
    kb._exec("UPDATE roles SET perms=? WHERE name='noc'", (json.dumps(["page.actions"]),))    # an admin narrows it later: stays narrowed
    rbac.Roles(kb)
    assert set(json.loads(kb._exec("SELECT perms FROM roles WHERE name='noc'")[0]["perms"])) == {"page.actions"}


def test_catalogue_and_templates():
    assert rbac.REQUIRES["act.case"] == "page.actions" and rbac.REQUIRES["act.publish"] == "page.auto"
    assert "act.policy" not in rbac.TEAM_PERMS and "act.execute" in rbac.TEAM_PERMS
    # product owner: no team template grants an act.* key; each is ticked by hand per team role (page.auto is view only)
    for k in ("manager", "operator", "viewer"):
        tp = rbac.TEAM_TEMPLATES[k]["perms"]
        assert "page.auto" in tp and not (tp & (rbac.AUTO_PERMS - {"page.auto"})), k
    assert rbac.FEATURES["page.auto"][1] == "view"
    assert rbac.normalize({"act.execute"}) >= {"act.execute", "page.actions"}
    secs = {s["id"]: [i["key"] for i in s["items"]] for s in rbac.catalogue()["sections"]}
    assert "page.auto" in secs["respond"] and "act.policy" in secs["respond"]


def _acc(role="viewer", all_=False, gp=(), members=(), owners=None):
    ms = tuple(Membership(team_id=t, team=f"T{t}", role_id=None, role_key="", role_label="", perms=frozenset(p), hosts=frozenset(),
                          service_rules=()) for t, p in members)
    return Access(email="u@x", role=role, all=all_, strict=False, global_perms=frozenset(gp), memberships=ms,
                  owners_fn=(lambda h: owners.get(h, [])) if owners is not None else None)


def test_can_approve_has_no_admin_bypass():
    owners = {"a-01": [1], "free-01": []}
    mgr = _acc(members=[(1, {"act.approve", "page.actions"})], owners=owners)
    assert mgr.can_approve("a-01") and not mgr.can_approve("free-01")
    other = _acc(members=[(2, {"act.approve", "page.actions"})], owners=owners)
    assert not other.can_approve("a-01")
    # scope.all user holding act.approve installation-wide: not on a team-owned host without act.policy
    wide = _acc(role="noc", all_=True, gp={"act.approve", "page.actions", "scope.all"}, owners=owners)
    assert wide.can_in(1, "act.approve")                         # what can_in would allow ...
    assert not wide.can_approve("a-01")                          # ... can_approve does not
    assert wide.can_approve("free-01")
    wide_pol = _acc(role="noc", all_=True, gp={"act.approve", "act.policy", "page.actions", "scope.all"}, owners=owners)
    assert wide_pol.can_approve("a-01")
    member_only = _acc(gp={"page.actions"}, members=[(1, {"page.actions"})], owners=owners)
    assert not member_only.can_approve("a-01")


# ---------------------------------------------------------------- API: team scope on kills, the act_disabled guard
@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email in ("mgra@corp.com", "opa@corp.com", "mgrb@corp.com", "opb@corp.com"):
        svc.users.register(email, PW, name=email.split("@")[0], role="viewer")
    with TestClient(a) as cl:
        cl.svc = svc
        yield cl


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


HAND = {"manager": {"act.case", "act.execute", "act.approve", "act.runbook", "act.publish", "act.promote", "act.kill", "act.golden", "act.audit"},
        "operator": {"act.case", "act.execute", "act.runbook", "act.kill"}}


def _teams(c):
    admin = login(c, *ADMIN)
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    tb = c.post("/api/teams", json={"name": "B", "rules": [{"field": "host", "op": "glob", "value": "b-*"}]}, headers=admin).json()
    rid = lambda t, k: next(r["id"] for r in t["roles"] if r["key"] == k)   # noqa: E731
    for t, email, key in ((ta, "mgra@corp.com", "manager"), (ta, "opa@corp.com", "operator"), (tb, "mgrb@corp.com", "manager"),
                          (tb, "opb@corp.com", "operator")):
        assert c.put(f"/api/teams/{t['id']}/members/{email}", json={"role_id": rid(t, key)}, headers=admin).status_code == 200
    for t in (ta, tb):                                                     # act.* keys are ticked by hand per team role
        for r in t["roles"]:
            if r["key"] in HAND:
                body = {"label": r["label"], "description": r.get("description", ""), "perms": sorted(set(r["perms"]) | HAND[r["key"]])}
                assert c.put(f"/api/teams/{t['id']}/roles/{r['id']}", json=body, headers=admin).status_code == 200
        c.svc.users.set_role(c.svc.users.get(email)["id"], "member", c.svc.roles.names())
    c.svc.inventory.upsert({"hostname": "a-app-01", "ip": "10.0.0.1"})
    c.svc.inventory.upsert({"hostname": "b-web-01", "ip": "10.0.0.2"})
    c.svc.teams.invalidate()
    return admin, ta, tb


def test_act_routes_disabled_while_off(c):
    admin = login(c, *ADMIN)
    st = c.get("/api/act/status", headers=admin)
    assert st.status_code == 200 and st.json()["mode"] == "off" and st.json()["custody"] == "none"
    for path in ("/api/act/kill", "/api/act/audit", "/api/act/policy", "/api/act/trust"):
        r = c.get(path, headers=admin)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "act_disabled", path
        assert r.json()["detail"]["tr"] and r.json()["detail"]["en"]
    r = c.post("/api/act/kill", json={"scope": "global", "reason": "x"}, headers=admin)
    assert r.status_code == 409


def test_team_operator_sees_only_their_team(c):
    admin, ta, tb = _teams(c)
    c.svc.act.set_mode("guided", by="admin@watchover.local")
    opa = login(c, "opa@corp.com")
    assert c.get("/api/act/status", headers=opa).status_code == 200
    r = c.post("/api/act/kill", json={"scope": "team", "ref": str(tb["id"]), "reason": "deneme"}, headers=opa)
    assert r.status_code == 404                                   # another team's stop: not even visible
    r = c.post("/api/act/kill", json={"scope": "host", "ref": "b-web-01", "reason": "deneme"}, headers=opa)
    assert r.status_code == 404
    r = c.post("/api/act/kill", json={"scope": "global", "reason": "deneme"}, headers=opa)
    assert r.status_code == 403
    r = c.post("/api/act/kill", json={"scope": "team", "ref": str(ta["id"]), "reason": "Değişiklik dondurma"}, headers=opa)
    assert r.status_code == 201, r.text
    kid = r.json()["id"]
    assert r.json()["effective_within_s"] == 12
    r = c.post("/api/act/kill", json={"scope": "host", "ref": "a-app-01", "reason": "disk"}, headers=opa)
    assert r.status_code == 201
    opb = login(c, "opb@corp.com")
    assert c.get("/api/act/kill", headers=opb).json() == []
    seen = c.get("/api/act/kill", headers=opa).json()
    assert {k["scope"] for k in seen} == {"team", "host"}
    st = c.get("/api/act/status", headers=opa).json()
    assert len(st["kill"]) == 2 and not st["killed_global"]       # the banner turns red for team A only
    assert c.get("/api/act/status", headers=opb).json()["kill"] == []
    # resume: an operator holds act.kill but not act.promote; the team manager resumes (team stops need a step-up)
    assert c.post(f"/api/act/kill/{kid}/resume", json={"reason": "bitti"}, headers=opa).status_code == 403
    mgra = login(c, "mgra@corp.com")
    r = c.post(f"/api/act/kill/{kid}/resume", json={"reason": "bitti"}, headers=mgra)
    assert r.status_code == 428 and r.json()["detail"]["code"] == "act_stepup_required"
    assert c.post("/api/auth/stepup/verify", json={"password": PW}, headers=mgra).json()["weak"] is True
    r = c.post(f"/api/act/kill/{kid}/resume", json={"reason": "bitti"}, headers=mgra)
    assert r.status_code == 200 and r.json()["active"] is False
    ev = [e["event"] for e in c.svc.act.audit.events(object_kind="kill")]
    assert ev.count("kill.on") == 2 and "kill.off" in ev


def test_global_stop_needs_two_to_resume(c):
    admin, ta, tb = _teams(c)
    c.svc.act.set_mode("guided", by="admin@watchover.local")
    r = c.post("/api/act/kill", json={"scope": "global", "reason": "tatbikat"}, headers=admin)
    assert r.status_code == 201
    kid = r.json()["id"]
    assert c.get("/api/act/status", headers=login(c, "opb@corp.com")).json()["killed_global"]
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=admin)
    r = c.post(f"/api/act/kill/{kid}/resume", json={"reason": "tamam"}, headers=admin)
    assert r.json() == {"id": kid, "active": True, "votes": 1, "need": 2}
    r = c.post(f"/api/act/kill/{kid}/resume", json={"reason": "tamam"}, headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "act_sod"
    c.svc.users.register("sec@corp.com", PW, name="sec", role="admin")
    sec = login(c, "sec@corp.com")
    c.post("/api/auth/stepup/verify", json={"password": PW}, headers=sec)
    r = c.post(f"/api/act/kill/{kid}/resume", json={"reason": "ikinci"}, headers=sec)
    assert r.json()["active"] is False and r.json()["votes"] == 2
