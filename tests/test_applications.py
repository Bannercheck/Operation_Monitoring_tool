"""Business applications (watchover.applications, /api/apps): the store (validation, rename carries threshold policies, delete
disables them), membership resolution (explicit, inventory value, pool, service globs, OTel namespace), discovery (proposals, nothing
merged silently, dismiss, explicit merge), health from the rollups (SLO, silent hosts, threshold breaches, anomalies), the threshold
precedence with the application scope, the app:<name> log filter, team isolation (API, logs, live tail, thresholds, MCP) and the
permissions (page.apps view, act.apps never granted implicitly)."""
import json
from datetime import datetime, timezone

import pytest

UTC = timezone.utc
ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    for k in ("DATABASE_URL", "MCP_API_KEY", "MCP_API_KEY_WRITE"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email, role in (("mgra@corp.com", "viewer"), ("opb@corp.com", "viewer"), ("view@corp.com", "viewer"), ("ops@corp.com", "operator")):
        svc.users.register(email, PW, name=email.split("@")[0].title(), role=role)
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def feed(svc, *rows):
    now = datetime.now(UTC).isoformat()
    svc.live.ingest("app.jsonl", ("\n".join(json.dumps({"ts": now, "level": "info", "service": "svc", **r}) for r in rows) + "\n").encode(), "seed")


def teams(c):
    """Team A (a-*) managed by mgra with act.apps ticked, team B (b-*) with opb as operator; lines on hosts of both."""
    svc, admin = c.svc, login(c, *ADMIN)
    feed(svc, *([{"host": "a-pay-01", "service": "pay-api", "msg": "AONLY pay ok 120 ms"}] * 5),
         *([{"host": "b-web-01", "service": "pay-api", "level": "error", "msg": "BSECRET pay failed"}] * 3),
         {"host": "b-web-01", "service": "web", "msg": "BWEB ok"})
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    tb = c.post("/api/teams", json={"name": "B", "rules": [{"field": "host", "op": "glob", "value": "b-*"}]}, headers=admin).json()
    rid = lambda t, k: next(r["id"] for r in t["roles"] if r["key"] == k)   # noqa: E731
    mgr = next(r for r in ta["roles"] if r["key"] == "manager")
    assert "page.apps" in mgr["perms"] and "act.apps" not in mgr["perms"]            # templates: view only, act.apps never implicit
    r = c.put(f"/api/teams/{ta['id']}/roles/{mgr['id']}", json={"label": mgr["label"], "perms": sorted(set(mgr["perms"]) | {"act.apps"})}, headers=admin)
    assert r.status_code == 200 and "act.apps" in r.json()["perms"], r.text
    for t, email, key in ((ta, "mgra@corp.com", "manager"), (tb, "opb@corp.com", "operator")):
        assert c.put(f"/api/teams/{t['id']}/members/{email}", json={"role_id": rid(t, key)}, headers=admin).status_code == 200
        svc.users.set_role(svc.users.get(email)["id"], "member", svc.roles.names())
    svc.teams.invalidate()
    return admin, ta, tb


# ---------------------------------------------------------------- store and membership
def test_store_validation_rename_and_delete(tmp_path):
    from watchover.applications import Applications, NameTaken, clean_members
    from watchover.db import Database
    from watchover.thresholds import Thresholds
    db = Database(str(tmp_path / "s.db"))
    st = Applications(db)
    thr = Thresholds(db, live=None)
    a = st.save({"name": "Ödeme", "criticality": "critical", "tags": "finans, demo", "slo_availability": 99.9, "members": {"hosts": "h1, h2", "services": ["pay-*"]}})
    assert a["tags"] == ["finans", "demo"] and a["members"]["hosts"] == ["h1", "h2"] and a["slo_availability"] == 99.9
    with pytest.raises(NameTaken):
        st.save({"name": "ödeme"})                                                 # names unique, case-insensitively
    for bad in ({"name": ""}, {"name": 'x"y'}, {"name": "x", "criticality": "urgent"}, {"name": "x", "slo_availability": 120},
                {"name": "x", "slo_p95_ms": -1}, {"name": "x", "members": {"flows": ["abc"]}}):
        with pytest.raises(ValueError):
            st.save(bad)
    assert clean_members({"hosts": ["a", "a", " "], "bogus": [1]})["hosts"] == ["a"]
    p = thr.save({"scope": "application", "scope_value": "Ödeme", "metric": "cpu", "warn": 70})
    st.save({**a, "name": "Payments"}, a["id"])
    assert thr.get(p["id"])["scope_value"] == "Payments"                          # a rename carries the policy along
    assert st.delete(a["id"]) == 1 and thr.get(p["id"])["enabled"] == 0           # delete disables, never re-targets
    assert st.list() == []


def test_membership_resolution_and_namespace_discovery(app):
    c, svc = app, app.svc
    svc.inventory.upsert({"hostname": "sap-01", "application": "SAP ERP", "env": "prod", "cluster": "sap-pool"})
    svc.inventory.upsert({"hostname": "sap-02", "application": "sap erp", "env": "prod"})
    svc.inventory.upsert({"hostname": "hana-01", "env": "prod", "cluster": "sap-pool"})
    feed(svc, {"host": "k8s-1", "service": "hr-api", "service.namespace": "hr", "msg": "x"}, {"host": "k8s-1", "service": "hr-worker", "service.namespace": "hr", "msg": "y"},
         {"host": "k8s-2", "service": "crm-web", "msg": "z"}, {"host": "k8s-2", "service": "crm-sync", "msg": "z"})
    svc.apps.flush_namespaces()
    svc.teams.invalidate(); svc.apps._uni = None; svc.apps._suni = None
    st = svc.apps
    a = st.save({"name": "ERP", "members": {"inv_apps": ["SAP ERP"], "pools": ["sap-pool"], "hosts": ["extra-01"]}})
    m = st.members(a)
    assert m.hosts == {"extra-01": "explicit", "sap-01": "inventory", "sap-02": "inventory", "hana-01": "pool"}
    b = st.save({"name": "HR", "members": {"namespaces": ["hr"], "services": ["HR-*"]}})
    mb = st.members(b)
    assert mb.svc_match("hr-api") and mb.svc_match("hr-worker") and mb.svc_match("HR-Other") and not mb.svc_match("crm-web")
    assert st.apps_of_host("sap-02") == ["ERP"] and st.apps_of_host("k8s-1") == []
    admin = login(c, *ADMIN)
    d = c.get("/api/apps/discovery", headers=admin).json()
    keys = {p["key"]: p for p in d["proposals"]}
    assert "svc:crm" in keys and keys["svc:crm"]["members"] == {"services": ["crm-*"]}
    assert "inv:sap erp" not in keys and "ns:hr" not in keys                       # covered by ERP / HR: no proposal
    assert {"k8s-2"} <= {h["host"] for h in d["unmatched_hosts"]} and "crm-web" in {s["service"] for s in d["unmatched_services"]}
    assert "hr-api" not in {s["service"] for s in d["unmatched_services"]}


def test_discovery_never_merges_silently(app):
    c, svc = app, app.svc
    admin = login(c, *ADMIN)
    svc.inventory.upsert({"hostname": "pay-01", "application": "Payments", "env": "prod"})
    svc.inventory.upsert({"hostname": "pay-02", "application": "Payments", "env": "prod"})
    svc.teams.invalidate(); svc.apps._uni = None
    keep = c.post("/api/apps", json={"name": "payments", "members": {"hosts": ["pay-09"]}}, headers=admin).json()
    p = next(x for x in c.get("/api/apps/discovery", headers=admin).json()["proposals"] if x["key"] == "inv:payments")
    assert p["exists"] == keep["id"] and p["hosts"] == ["pay-01", "pay-02"]
    r = c.post("/api/apps/discovery/accept", json={"key": "inv:payments"}, headers=admin)
    assert r.status_code == 409                                                    # same name: refused, the existing one untouched
    assert c.get(f"/api/apps/{keep['id']}", headers=admin).json()["members"]["inv_apps"] == []
    r = c.post("/api/apps/discovery/accept", json={"key": "inv:payments", "into": keep["id"]}, headers=admin)
    assert r.status_code == 201 and r.json()["members"]["inv_apps"] == ["Payments"] and r.json()["members"]["hosts"] == ["pay-09"]
    svc.inventory.upsert({"hostname": "crm-01", "application": "CRM"})
    svc.teams.invalidate(); svc.apps._uni = None
    r = c.post("/api/apps/discovery/accept", json={"key": "inv:crm", "name": "Müşteri ilişkileri"}, headers=admin)
    assert r.status_code == 201 and r.json()["members"] == {**r.json()["members"], "inv_apps": ["CRM"]}
    svc.inventory.upsert({"hostname": "bi-01", "application": "BI"})
    svc.teams.invalidate(); svc.apps._uni = None
    assert c.post("/api/apps/discovery/dismiss", json={"key": "inv:bi"}, headers=admin).status_code == 200
    assert "inv:bi" not in {x["key"] for x in c.get("/api/apps/discovery", headers=admin).json()["proposals"]}
    assert c.post("/api/apps/discovery/accept", json={"key": "inv:bi"}, headers=admin).status_code == 404


# ---------------------------------------------------------------- health
def test_health_from_rollups_slo_silence_alerts(app):
    c, svc = app, app.svc
    admin = login(c, *ADMIN)
    feed(svc, *([{"host": "pay-01", "service": "pay-api", "msg": "GET /pay 200 120 ms"}] * 18), *([{"host": "pay-01", "service": "pay-api", "level": "error",
                                                                                                  "msg": "GET /pay 500 900 ms"}] * 2),
         *([{"host": "edge-01", "service": "pay-gw", "msg": "proxy 30 ms"}] * 4), {"host": "other-01", "service": "web", "msg": "unrelated"})
    svc.live.rollup.last_seen["pay-02"] = svc.live.rollup.last_seen["pay-01"] - 900          # seen 15 minutes ago: silent
    a = c.post("/api/apps", json={"name": "Pay", "slo_availability": 99.0, "slo_p95_ms": 2000, "criticality": "critical",
                                  "members": {"hosts": ["pay-01", "pay-02", "pay-03"], "services": ["pay-gw"]}}, headers=admin).json()
    h = c.get("/api/apps", headers=admin).json()["apps"][0]["health"]
    assert h["logs"]["lines"] == 24 and h["logs"]["errors"] == 2                   # pay-01 (20) + pay-gw on edge-01 (4); other-01 not counted
    assert h["hosts"] == {"total": 3, "reporting": 1, "silent": 1, "unknown": 1, "via_service": 1}
    assert h["slo"]["state"] == "breach" and h["status"] == "crit" and "slo_breach" in h["reasons"] and "silent" in h["reasons"]
    assert h["worst_host"]["host"] == "pay-01" and len(h["spark"]) == 20 and sum(h["spark"]) == 24
    d = c.get(f"/api/apps/{a['id']}", headers=admin).json()
    assert {x["host"]: x["state"] for x in d["hosts"]} == {"pay-01": "reporting", "pay-02": "silent", "pay-03": "unknown"}
    assert [x["host"] for x in d["service_hosts"]] == ["edge-01"] and d["log_query"] == "app:Pay"
    svcs = {x["service"]: x for x in d["services"]}
    assert svcs["pay-gw"]["member"] and svcs["pay-gw"]["lines"] == 4 and not svcs["pay-api"]["member"] and svcs["pay-api"]["lines"] == 20
    s = c.get(f"/api/apps/{a['id']}/series?minutes=15", headers=admin).json()
    assert s["source"] == "live" and sum(x["lines"] for x in s["series"]) == 24 and len(s["series"]) == 15
    assert c.get(f"/api/apps/{a['id']}/series?minutes=1440", headers=admin).json()["source"] == "history"
    # an open threshold breach and an anomaly on a member host / service
    svc.kb._exec("INSERT INTO threshold_state (host, metric, level, since, value, limit_value, updated_at) VALUES ('pay-03','cpu','crit','x',97,90,'x')")
    svc.anomalies._upsert("metric", "k1", env="prod", host="pay-03", metric="cpu", title="cpu", detail="", observed=1, baseline=0, spread=0, score=5, series=[1])
    svc.apps._alerts = None; svc.apps._cache.clear()
    h = c.get("/api/apps", headers=admin).json()["apps"][0]["health"]
    assert h["alerts"]["crit"] == 1 and h["alerts"]["anomalies"] == 1 and "thr_crit" in h["reasons"]
    assert len(c.get(f"/api/apps/{a['id']}/events", headers=admin).json()["events"]) == 1
    assert c.get(f"/api/apps/{a['id']}/thresholds", headers=admin).json()["states"][0]["host"] == "pay-03"


# ---------------------------------------------------------------- thresholds
def test_threshold_precedence_with_application_scope(app):
    c, svc = app, app.svc
    admin = login(c, *ADMIN)
    st, thr = svc.apps, svc.thresholds
    st.save({"name": "Shop", "members": {"hosts": ["db-01", "web-01"]}})
    st.save({"name": "Billing", "members": {"hosts": ["db-01"]}})
    mk = lambda scope, value, warn, **kw: thr.save({"scope": scope, "scope_value": value, "metric": "cpu", "warn": warn, "crit": 99, **kw})   # noqa: E731
    mk("all", "", 90); mk("env", "prod", 85); mk("pool", "db", 80)
    app_shop = mk("application", "Shop", 75)
    eff = lambda h: thr.effective(h, "prod", "db")["cpu"]                         # noqa: E731
    assert eff("web-01")["id"] == app_shop["id"]                                   # application > pool > env > all
    app_bill = mk("application", "Billing", 60)
    assert eff("db-01")["id"] == app_bill["id"]                                    # a shared host: the strictest application level wins
    mk("application", "Shop", 50, name="newer stricter")
    assert eff("db-01")["warn"] == 50
    g = mk("glob", "db-*", 70)
    assert eff("db-01")["id"] == g["id"]                                           # host pattern > application
    h = mk("host", "db-01", 65)
    assert eff("db-01")["id"] == h["id"]                                           # host > host pattern
    assert thr.effective("other-01", "prod", "x")["cpu"]["scope"] == "env"        # not a member: the application policy does not apply
    # the alert names the application(s)
    sent = []
    svc.notifier.dispatch = lambda rule, key, title, body, force=False: sent.append((title, body)) or {"ok": True}
    thr._notify({**app_shop, "for_min": 0}, "db-01", "cpu", "prod", 91.0, "crit", 75.0)
    assert "[Billing, Shop]" in sent[0][0] and "Application: Billing, Shop" in sent[0][1]
    # the API checks the application exists and is visible; meta lists the names
    r = c.post("/api/thresholds", json={"scope": "application", "scope_value": "nope", "metric": "cpu", "warn": 50}, headers=admin)
    assert r.status_code == 400
    r = c.post("/api/thresholds", json={"scope": "application", "scope_value": "shop", "metric": "memory", "warn": 50}, headers=admin)
    assert r.status_code == 201 and r.json()["scope_value"] == "Shop"
    assert set(c.get("/api/thresholds/meta", headers=admin).json()["applications"]) == {"Shop", "Billing"}
    assert "application" in c.get("/api/thresholds/meta", headers=admin).json()["scopes"]


# ---------------------------------------------------------------- log query
def test_app_filter_in_the_log_query_language(app):
    from watchover import logquery as lq
    c, svc = app, app.svc
    admin = login(c, *ADMIN)
    feed(svc, {"host": "pay-01", "service": "x", "msg": "P1"}, {"host": "gw-01", "service": "pay-gw", "msg": "P2"}, {"host": "gw-01", "service": "web", "msg": "N1"},
         {"host": "other", "service": "legacy-svc", "msg": "L1"})
    c.post("/api/apps", json={"name": "Pay Ops", "members": {"hosts": ["pay-01"], "services": ["pay-*"]}}, headers=admin)
    c.post("/api/apps", json={"name": "Empty"}, headers=admin)
    msgs = lambda q: sorted(x["message"] for x in c.get("/api/logs/search", params={"q": q, "last": 15}, headers=admin).json()["lines"])   # noqa: E731
    assert msgs('app:"Pay Ops"') == ["P1", "P2"]
    assert msgs('application:"pay ops"') == ["P1", "P2"]                           # alias, case-insensitive name
    assert "P1" not in msgs('-app:"Pay Ops"') and "N1" in msgs('-app:"Pay Ops"')
    assert msgs("app:Pay*") == ["P1", "P2"]                                        # a name pattern
    assert msgs("app:Empty") == []                                                 # an application without members matches nothing
    r = c.get("/api/logs/search", params={"q": "app:legacy-svc", "last": 15}, headers=admin).json()
    assert [x["message"] for x in r["lines"]] == ["L1"] and r["query"]["warnings"][0]["code"] == "app_unknown"   # the old service alias
    assert r["query"]["normalized"] == "app:legacy-svc"
    t = c.get("/api/logs/tail", params={"q": 'app:"Pay Ops"'}, headers=admin).json()
    assert sorted(x["message"] for x in t["lines"]) == ["P1", "P2"]                 # live tail too
    f = next(x for x in c.get("/api/logs/fields", headers=admin).json()["fields"] if x["name"] == "app")
    assert set(f["values"]) == {"Pay Ops", "Empty"}
    node, warns, fp = lq.expand_apps(lq.parse("app:x level:error").node, None)       # no store: the old meaning, no warning
    assert lq.normalize(node) == "service:x level:ERROR" and not warns and not fp


# ---------------------------------------------------------------- teams
def test_team_cannot_see_another_teams_application(app):
    c, svc = app, app.svc
    admin, ta, tb = teams(c)
    ha, hb = login(c, "mgra@corp.com"), login(c, "opb@corp.com")
    r = c.post("/api/apps", json={"name": "A-Pay", "members": {"hosts": ["a-pay-01"], "services": ["pay-*"]}}, headers=ha)
    assert r.status_code == 201 and r.json()["team_id"] == ta["id"]                 # a team manager's application is the team's
    aid = r.json()["id"]
    assert c.post("/api/apps", json={"name": "x", "members": {"hosts": ["b-web-01"]}}, headers=ha).status_code == 400   # not their host
    assert c.post("/api/apps", json={"name": "x", "team_id": tb["id"]}, headers=ha).status_code == 403
    assert c.post("/api/apps", json={"name": "x"}, headers=hb).status_code == 403   # B's operator: no act.apps
    # B: the application does not exist
    assert c.get("/api/apps", headers=hb).json()["apps"] == []
    for p in ("", "/series", "/events", "/thresholds", "/traces", "/topology"):
        assert c.get(f"/api/apps/{aid}{p}", headers=hb).status_code == 404, p
    assert c.put(f"/api/apps/{aid}", json={"name": "z"}, headers=hb).status_code in (403, 404)
    lines = [x["message"] for x in c.get("/api/logs/search", params={"q": "app:A-Pay", "last": 15}, headers=hb).json()["lines"]]
    assert lines == [] and not any("AONLY" in x for x in lines)                     # unknown to B: the service alias, B's own hosts only
    tail = c.get("/api/logs/tail", params={"q": "app:A-Pay"}, headers=hb).json()
    assert not any("AONLY" in x["message"] for x in tail["lines"])
    r = c.post("/api/thresholds", json={"scope": "application", "scope_value": "A-Pay", "metric": "cpu", "warn": 50}, headers=hb)
    assert r.status_code in (400, 403)
    # A sees its application, computed over its own hosts only: B's pay-api errors on b-web-01 never count
    h = c.get("/api/apps", headers=ha).json()["apps"][0]["health"]
    assert h["logs"]["lines"] == 5 and h["logs"]["errors"] == 0 and h["hosts"]["via_service"] == 0
    a_lines = [x["message"] for x in c.get("/api/logs/search", params={"q": "app:A-Pay", "last": 15}, headers=ha).json()["lines"]]
    assert a_lines and all("AONLY" in x for x in a_lines)
    # an installation-wide application shows to a team only with the team's own part of it
    shared = c.post("/api/apps", json={"name": "Shared", "members": {"hosts": ["a-pay-01", "b-web-01"]}}, headers=admin).json()
    rb = c.get(f"/api/apps/{shared['id']}", headers=hb).json()
    assert rb["members"]["hosts"] == ["b-web-01"] and [x["host"] for x in rb["hosts"]] == ["b-web-01"] and rb["editable"] is False
    assert c.put(f"/api/apps/{shared['id']}", json={"name": "Shared2"}, headers=ha).status_code == 403
    assert {a["name"] for a in c.get("/api/apps", headers=hb).json()["apps"]} == {"Shared"}
    # a manager edit keeps the explicit hosts they cannot see
    own = c.post("/api/apps", json={"name": "Mixed", "team_id": ta["id"], "members": {"hosts": ["a-pay-01", "b-web-01"]}}, headers=admin).json()
    assert c.put(f"/api/apps/{own['id']}/members", json={"hosts": ["a-pay-01"]}, headers=ha).status_code == 200
    assert svc.apps.get(own["id"])["members"]["hosts"] == ["a-pay-01", "b-web-01"]
    # claim: ownership does not open hosts; the team gate decides (another team's host never moves)
    r = c.post(f"/api/apps/{own['id']}/claim-hosts", headers=ha).json()
    assert "b-web-01" not in r["added"] and r["rejected_hidden"] == 1 and r["rejected"] == []
    assert "b-web-01" not in svc.teams.get(ta["id"])["matched"]


def test_mcp_application_tools_follow_team_scope(app):
    c, svc = app, app.svc
    admin, ta, tb = teams(c)
    c.post("/api/apps", json={"name": "A-Pay", "team_id": ta["id"], "members": {"hosts": ["a-pay-01"]}}, headers=admin)
    c.post("/api/apps", json={"name": "B-Web", "team_id": tb["id"], "members": {"hosts": ["b-web-01"]}}, headers=admin)
    tok = c.post("/api/mcp/keys", json={"name": "kb", "kind": "team", "team_id": tb["id"]}, headers=admin).json()["token"]

    def tool(name, args=None):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
                   headers={"Authorization": f"Bearer {tok}"}).json()["result"]
        return r["isError"], json.loads(r["content"][0]["text"])
    names = {t["name"] for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {tok}"}).json()["result"]["tools"]}
    assert {"list_applications", "application_health"} <= names
    err, out = tool("list_applications")
    assert not err and [a["name"] for a in out["applications"]] == ["B-Web"] and "AONLY" not in json.dumps(out)
    err, out = tool("application_health", {"app": "B-Web"})
    assert not err and out["hosts"][0]["host"] == "b-web-01" and out["log_query"] == "app:B-Web"
    for ref in ("A-Pay", str(svc.apps.by_name("A-Pay")["id"])):                 # another team's application, by name or by id: not found
        err, out = tool("application_health", {"app": ref})
        assert err and out["error"] == "not_found" and out["message"] == f"application not found: {ref}"


def test_permissions_view_and_manage(app):
    c, svc = app, app.svc
    from watchover import rbac
    assert "page.apps" in svc.roles.perms("viewer") and "page.apps" in svc.roles.perms("operator")
    assert "act.apps" not in svc.roles.perms("operator") and all("act.apps" not in t["perms"] for t in rbac.TEAM_TEMPLATES.values())
    assert rbac.REQUIRES["act.apps"] == "page.apps" and rbac.normalize({"act.apps"}) == {"act.apps", "page.apps"}
    hv, ho, admin = login(c, "view@corp.com"), login(c, "ops@corp.com"), login(c, *ADMIN)
    assert c.get("/api/apps", headers=hv).status_code == 200
    assert c.post("/api/apps", json={"name": "x"}, headers=ho).status_code == 403
    a = c.post("/api/apps", json={"name": "X", "tags": ["t"]}, headers=admin).json()
    assert c.delete(f"/api/apps/{a['id']}", headers=hv).status_code == 403
    assert c.get("/api/apps", headers=hv).json()["apps"][0]["editable"] is False
    svc.roles.save("viewer", svc.roles.perms("viewer") - {"page.apps"})
    svc.access.invalidate()
    assert c.get("/api/apps", headers=hv).status_code == 403


def test_apps_v1_migration_grants_view_only(tmp_path):
    import json as js
    from watchover.db import Database
    from watchover.rbac import Roles
    db = Database(str(tmp_path / "m.db"))
    Roles(db)
    db._exec("DELETE FROM rbac_meta WHERE key='apps_v1'")
    db._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('noc', 'NOC', '', ?, 0)", (js.dumps(["page.ops", "act.thresholds"]),))
    db._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('sec', 'Sec', '', ?, 0)", (js.dumps(["page.scan"]),))
    r = Roles(db)
    assert r.perms("noc") == {"page.ops", "act.thresholds", "page.apps"} and r.perms("sec") == {"page.scan"}


def test_demo_seed_only_in_demo_mode(app, monkeypatch):
    from watchover import demo as wo_demo_mod
    svc = app.svc
    monkeypatch.setattr(wo_demo_mod, "SIMULATOR", False)
    assert svc.apps.seed_demo() == 0 and svc.apps.list() == []
    monkeypatch.setattr(wo_demo_mod, "SIMULATOR", True)
    assert svc.apps.seed_demo() == 4 and {a["origin"] for a in svc.apps.list()} == {"demo"}
    from watchover import demo
    out = demo.clean(svc, dry_run=False)
    assert out["counts"].get("applications") == 4 and svc.apps.list() == []


@pytest.mark.skipif(not __import__("os").environ.get("WATCHOVER_TEST_DATABASE_URL"), reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_applications_on_postgres(tmp_path, monkeypatch):
    """The same store, health, namespace upsert, threshold rename, discovery and app: query on PostgreSQL."""
    import os
    from watchover.db import Database
    url = os.environ["WATCHOVER_TEST_DATABASE_URL"]
    d = Database(url); d._exec("DROP SCHEMA public CASCADE"); d._exec("CREATE SCHEMA public"); d.close()
    for k, v in {"DATABASE_URL": url, "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k", "WATCHOVER_LOG_STORE": "db"}.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    assert svc.kb.pg
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as c:
        admin = login(c, *ADMIN)
        svc.inventory.upsert({"hostname": "pg-01", "application": "Ödeme", "env": "prod"})
        feed(svc, *([{"host": "pg-01", "service": "pay-api", "service.namespace": "pay", "msg": "ok 40 ms"}] * 4), {"host": "k8s-9", "service": "pay-web", "service.namespace": "pay", "msg": "x"})
        assert svc.apps.flush_namespaces() == 2 and svc.apps.flush_namespaces() == 0
        feed(svc, {"host": "k8s-9", "service": "pay-web", "service.namespace": "pay", "msg": "y"})
        assert svc.apps.flush_namespaces() == 1                                    # the upsert path (ON CONFLICT) on PostgreSQL
        svc.teams.invalidate(); svc.apps._uni = None; svc.apps._suni = None
        r = c.post("/api/apps", json={"name": "Ödeme", "members": {"inv_apps": ["ödeme"], "namespaces": ["pay"]}, "tags": ["x"], "slo_availability": 99}, headers=admin)
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        assert c.post("/api/apps", json={"name": "ÖDEME"}, headers=admin).status_code == 409
        h = c.get("/api/apps", headers=admin).json()["apps"][0]["health"]
        assert h["logs"]["lines"] == 6 and h["hosts"]["total"] == 1
        p = c.post("/api/thresholds", json={"scope": "application", "scope_value": "ödeme", "metric": "cpu", "warn": 50}, headers=admin).json()
        assert c.put(f"/api/apps/{aid}", json={"name": "Payments"}, headers=admin).status_code == 200
        assert svc.thresholds.get(p["id"])["scope_value"] == "Payments"
        msgs = [x["message"] for x in c.get("/api/logs/search", params={"q": "app:Payments", "last": 15}, headers=admin).json()["lines"]]
        assert len(msgs) == 6
        assert c.get("/api/apps/discovery", headers=admin).status_code == 200
        assert c.get(f"/api/apps/{aid}/series?minutes=1440", headers=admin).json()["source"] == "history"
        assert c.delete(f"/api/apps/{aid}", headers=admin).json()["policies_disabled"] == 1


def test_application_integration_map_api(app):
    """GET /api/apps/{id}/integrations: the member servers' flows folded into one map; peers carry their applications and IPs."""
    import random
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD
    from watchover.live import simulate_flows
    c, svc = app, app.svc
    admin = login(c, INITIAL_EMAIL, INITIAL_PASSWORD)
    svc.inventory.upsert({"hostname": "db-01", "ip": "172.16.1.55", "env": "prod"})
    svc.inventory.upsert({"hostname": "kafka-01", "ip": "172.16.1.70", "env": "prod"})
    svc.flows._names_at = 0
    svc.live.ingest("flows.jsonl", simulate_flows(random.Random(3)), "flow-agent")
    api = c.post("/api/apps", json={"name": "Ödeme", "members": {"hosts": ["prd-api-01", "prd-api-02"]}}, headers=admin).json()
    c.post("/api/apps", json={"name": "Veri", "members": {"hosts": ["db-01"]}}, headers=admin)
    m = c.get(f"/api/apps/{api['id']}/integrations?minutes=60", headers=admin).json()
    assert m["app"] and m["host"] == "Ödeme" and {x["host"] for x in m["members"]} == {"prd-api-01", "prd-api-02"}
    db = next(o for o in m["outbound"] if o["target"] == "db-01")
    assert db["ip"] == "172.16.1.55" and db["apps"] == ["Veri"] and set(db["via"]) <= {"prd-api-01", "prd-api-02"}
    assert c.get("/api/apps/999/integrations", headers=admin).status_code == 404
