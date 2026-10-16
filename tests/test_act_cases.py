"""WP6a: triggers -> cases (§4.1, §4.2, §4.5, §4.6): merge, reopen + relapse, loop guard, storm, flood, owning team, one breach
= one trigger, dataset first actions stay plain, the three-host rule, the inbox order, the legacy API untouched."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from watchover.act.cases import family_of, inbox_score, resource_hint
from watchover.act.types import Trigger

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


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
    with TestClient(a) as cl:
        cl.svc = svc
        yield cl


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def on(c):
    c.svc.act.set_mode("guided", by="admin@watchover.local")
    return c.svc.act


def cases(c):
    return c.svc.kb._exec("SELECT * FROM actions WHERE kind='case' ORDER BY id")


def breach(svc, host="web-prod-07", metric="disk", v=93.4):
    return svc.anomalies._upsert("metric", f"threshold:prod:{host}:{metric}", env="prod", host=host, metric=metric,
                                 title=f"{host}: {metric} {v:.0f}% ≥ 90%", detail=f"{metric} is {v} % on {host}; critical level 90 %",
                                 observed=v, baseline=90.0, spread=0.0, score=5.0, series=[v])


def test_breach_and_log_query_make_one_case(c):
    act = on(c)
    breach(c.svc)
    breach(c.svc, v=94.0)                       # the anomaly is still open: on_open fires once, the breach arrives once
    assert len(act.queue) == 1
    act.drain()
    (case,) = cases(c)
    assert case["source"] == "threshold" and case["family"] == "metric:disk" and case["phase"] == "ready" and case["status"] == "open"
    assert case["severity"] == "critical" and case["priority"] == "P1" and case["hits"] == 1 and case["env_tier"] == "prod"
    rule = {"id": 41, "name": "Disk dolu", "query": '"No space left on device"', "open_case": 1, "severity": "high", "window_min": 5}
    count = {"n": 3, "hosts": [("web-prod-07", 3)], "samples": ["02:11:07 web-prod-07 write failed: No space left on device (/var/log/app/x.log)"]}
    assert act.from_query(rule, count) == 1
    act.drain()
    (case,) = cases(c)                          # same host, family metric:disk within 30 min: merged, no second case
    assert case["hits"] == 2
    trig = c.svc.kb._exec("SELECT source, key FROM act_triggers WHERE case_id=? ORDER BY id", (case["id"],))
    assert [t["source"] for t in trig] == ["threshold", "query"] and trig[1]["key"] == "query:41:web-prod-07"
    # a later breach of the same key (the anomaly closed and opened again) increments hits
    c.svc.kb._exec("UPDATE anomalies SET status='resolved'")
    breach(c.svc, v=95.0)
    act.drain()
    assert cases(c)[0]["hits"] == 3 and len(cases(c)) == 1
    ev = [e["event"] for e in act.audit.events(object_kind="case")]
    assert ev.count("case.opened") == 1 and ev.count("trigger.merged") == 2


def test_three_hosts_rule_and_open_case_flag(c):
    act = on(c)
    rule = {"id": 7, "name": "Hata seli", "query": "level:error", "open_case": 1}
    many = {"n": 400, "hosts": [("a", 200), ("b", 150), ("c", 50)], "samples": []}
    trigs = act.cases.triggers_from_query(rule, many)
    assert [t.key for t in trigs] == ["query:7:*"] and trigs[0].host == ""
    two = act.cases.triggers_from_query(rule, {"n": 9, "hosts": [("a", 5), ("b", 4)], "samples": []})
    assert [t.key for t in two] == ["query:7:a", "query:7:b"]
    assert act.from_query(dict(rule, open_case=0), many) == 0          # only rules ticked "Vaka aç"
    # the LogWatch hook runs after the dispatch
    seen = []
    c.svc.logwatch.on_case = lambda r, cnt: seen.append((r["id"], cnt["n"]))
    c.svc.logwatch.count = lambda r, scope: {"n": 5, "ready": True, "hosts": [("a", 5)], "samples": [], "start": datetime.now(timezone.utc),
                                             "end": datetime.now(timezone.utc)}
    c.svc.logwatch.owner_fn = lambda owner: (True, None)
    c.svc.logwatch.evaluate({"id": 9, "name": "x", "query": "error", "threshold": 1, "open_case": 1, "severity": "high", "channels": "",
                             "targets": "", "cooldown_min": 0}, c.svc.notifier)
    assert seen == [(9, 5)]


def test_reopen_relapse_and_prev(c):
    act = on(c)
    t = Trigger(source="manual", key="external:disk:web-01", host="web-01", severity="high", title="Disk")
    cid = act.cases.route(t)["case_id"]
    c.svc.kb._exec("UPDATE actions SET run_id=388, runbook_key='linux-varlog-full' WHERE id=?", (cid,))
    act.cases.transition(cid, "resolved", "op@corp.com", outcome="ok")
    r = act.cases.route(t)
    assert r == {"case_id": cid, "action": "reopened", "relapse_run": 388}
    row = act.cases.get(cid)
    assert row["phase"] == "ready" and row["status"] == "open" and row["reopened"] == 1 and row["closed_at"] == ""
    ev = c.svc.kb._exec("SELECT outcome, penalty, run_id FROM act_trust_events WHERE case_id=?", (cid,))
    assert ev == [{"outcome": "relapse", "penalty": 0.5, "run_id": 388}]
    act.cases.transition(cid, "resolved", "op@corp.com")
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
    c.svc.kb._exec("UPDATE actions SET closed_at=? WHERE id=?", (old, cid))
    r2 = act.cases.route(t)
    assert r2["action"] == "opened" and r2["case_id"] != cid
    assert act.cases.get(r2["case_id"])["prev_id"] == cid
    assert act.cases.detail(r2["case_id"])["history"]["previous"] == 1


def test_loop_guard(c):
    act = on(c)
    t = Trigger(source="external", key="external:svc:web-02", host="web-02", severity="medium", title="Servis düştü")
    cid = act.cases.route(t)["case_id"]
    for _ in range(2):
        act.cases.transition(cid, "resolved", "op@corp.com")
        assert act.cases.route(t)["action"] == "reopened"
    row = act.cases.get(cid)
    assert row["needs_human"] == 1 and row["next_step"] == "Tekrarlıyor: otomatik çözüm kalıcı değil"
    follow = c.svc.kb._exec("SELECT title, kind FROM actions WHERE evidence LIKE 'loop:%'")
    assert len(follow) == 1 and follow[0]["kind"] == "action" and follow[0]["title"].startswith("Kalıcı çözüm")


def test_storm_parent(c):
    act = on(c)
    c.svc._pools = (9e12, {f"app-lnx-0{i}": "app-lnx" for i in range(1, 8)})
    for i in range(1, 7):
        act.cases.route(Trigger(source="external", key=f"external:x:app-lnx-0{i}", host=f"app-lnx-0{i}", severity="high", title=f"x{i}"))
    parent = c.svc.kb._exec("SELECT * FROM actions WHERE case_key='storm:pool:app-lnx'")
    assert len(parent) == 1 and parent[0]["storm"] == 1 and parent[0]["title"] == "Fırtına: app-lnx havuzu · 6 vaka"
    kids = c.svc.kb._exec("SELECT storm FROM actions WHERE parent_id=?", (parent[0]["id"],))
    assert len(kids) == 6 and all(k["storm"] == 1 for k in kids)
    inbox = act.cases.inbox("admin@watchover.local", [], lambda r: True)
    assert [x["id"] for x in inbox if x["kind"] == "case"] == [parent[0]["id"]] and inbox[0]["children"] == 6


def test_flood_fold(c):
    act = on(c)
    act.policy.set("act_cases_per_min", 3, by="admin@watchover.local")
    res = [act.cases.route(Trigger(source="external", key=f"external:f:{i}", host=f"h{i}", title=f"f{i}"))["action"] for i in range(6)]
    assert res == ["opened", "opened", "opened", "flood", "flood", "flood"]
    flood = c.svc.kb._exec("SELECT * FROM actions WHERE case_key LIKE 'flood:%'")
    assert len(flood) == 1 and flood[0]["hits"] == 3 and flood[0]["title"].startswith("Sinyal seli")
    assert len(cases(c)) == 4


def test_owning_team(c):
    act = on(c)
    admin = login(c, *ADMIN)
    ta = c.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    c.svc.inventory.upsert({"hostname": "a-app-01", "ip": "10.0.0.1"})
    c.svc.inventory.upsert({"hostname": "free-01", "ip": "10.0.0.2"})
    c.svc.teams.invalidate()
    a = act.cases.route(Trigger(source="external", key="external:o:a", host="a-app-01", title="a"))["case_id"]
    f = act.cases.route(Trigger(source="external", key="external:o:f", host="free-01", title="f"))["case_id"]
    r = act.cases.route(Trigger(source="external", key="external:o:r", host="free-01", title="r", team_id=ta["id"]))["case_id"]
    assert act.cases.get(a)["team_id"] == ta["id"] and act.cases.get(f)["team_id"] is None and act.cases.get(r)["team_id"] == ta["id"]


def test_dataset_first_actions_never_cases(c):
    from watchover.analysis import Analysis
    from watchover.pipeline import ingest_path
    act = on(c)
    obs, report = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    a = Analysis(obs, report)
    n = c.svc.datasets.record_first_actions(a, "demo")
    act.drain()
    assert n > 0 and cases(c) == []
    rows = c.svc.kb._exec("SELECT kind FROM actions WHERE evidence LIKE 'first:%'")
    assert len(rows) == n and {r["kind"] for r in rows} == {"action"}
    assert c.svc.datasets.record_first_actions(a, "demo") == 0           # dedup reads plain actions only


def test_hooks_are_noops_while_off(c):
    act = c.svc.act
    assert act.mode == "off"
    breach(c.svc)
    assert act.from_query({"id": 1, "open_case": 1}, {"n": 1, "hosts": [("a", 1)]}) == 0
    assert act.from_analysis(object()) == 0 and act.check_bizflows() == 0
    assert len(act.queue) == 0 and cases(c) == []


def test_emit_never_raises(c):
    act = on(c)
    act.cases.route = lambda t: (_ for _ in ()).throw(RuntimeError("db down"))
    assert act.emit(Trigger(source="manual", key="manual:1")) is True
    assert act.drain() == 1 and act.stats["errors"] == 1
    assert act.from_anomaly({"key": None}) is False and act.from_anomaly("garbage") is False


def test_inbox_order_formula():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(minutes=20)).isoformat()
    me = "op@corp.com"
    base = {"severity": "high", "first_seen": ts, "phase": "ready", "team_id": 4}
    assert inbox_score(dict(base, owner=""), me, [4], now) == 60 + 20 + 10
    assert inbox_score(dict(base, owner=me), me, [4], now) == 60 + 50 + 10
    assert inbox_score(dict(base, owner="x@y", approval_for_me=True), me, [4], now) == 60 + 80 + 10
    assert inbox_score(dict(base, owner="x@y", phase="waiting_human"), me, [4], now) == 60 + 30 + 10
    assert inbox_score(dict(base, owner="x@y", first_seen=(now - timedelta(hours=5)).isoformat()), me, [4], now) == 60 + 30
    assert inbox_score(dict(base, owner="x@y", handled=True), me, [4], now) == 60 + 10 - 100
    assert inbox_score({"priority": "P1", "first_seen": now.isoformat(), "team_id": 9, "owner": ""}, me, [4], now) == 100


def test_family_and_hints():
    assert resource_hint("write failed: No space left on device (/var/log/a)") == "metric:disk"
    assert family_of(Trigger(source="threshold", key="threshold:prod:h:cpu", facts={"metric": "cpu"})) == "metric:cpu"
    assert family_of(Trigger(source="query", key="query:3:h", facts={"rule_id": 3})) == "logq:3"
    assert family_of(Trigger(source="incident", key="incident:x", facts={"root_template": "OOM"})).startswith("tpl:")


def test_api_inbox_case_close_and_legacy(c):
    h = login(c, *ADMIN)
    assert c.get("/api/act/inbox", headers=h).status_code == 409                 # act_mode = off
    on(c)
    plain = c.post("/api/actions", json={"incident_id": "INC-1", "title": "Plain"}, headers=h).json()
    r = c.post("/api/act/cases", json={"title": "Disk dolu: /var/log (web-prod-07)", "host": "web-prod-07", "severity": "high"}, headers=h)
    assert r.status_code == 201 and r.json()["phase"] == "ready" and r.json()["routed"] == "opened"
    cid = r.json()["id"]
    ib = c.get("/api/act/inbox?tab=team", headers=h).json()
    assert [x["id"] for x in ib] == [cid, plain["id"]] and ib[0]["priority_score"] >= ib[1]["priority_score"]   # ties: newest first
    assert ib[0]["kind"] == "case" and ib[1]["kind"] == "action"
    d = c.get(f"/api/act/cases/{cid}", headers=h).json()
    assert d["subject"]["host"] == "web-prod-07" and [e["event"] for e in d["timeline"]][:2] == ["proposal.sealed", "case.opened"] and d["action_id"] == cid
    # the legacy actions API: cases are hidden by default and cannot be closed through PATCH
    assert [a["id"] for a in c.get("/api/actions", headers=h).json()] == [plain["id"]]
    assert cid in [a["id"] for a in c.get("/api/actions?kind=all", headers=h).json()]
    r = c.patch(f"/api/actions/{cid}", json={"status": "done"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "act_case_managed"
    assert c.delete(f"/api/actions/{cid}", headers=h).status_code == 409
    assert c.patch(f"/api/actions/{plain['id']}", json={"status": "done"}, headers=h).status_code == 200
    assert c.post(f"/api/act/cases/{cid}/assign", json={}, headers=h).json()["owner"] == "admin@watchover.local"
    assert c.get("/api/act/inbox?tab=mine", headers=h).json()[0]["id"] == cid
    r = c.post(f"/api/act/cases/{cid}/close", json={"outcome": "unverified", "what_did_you_do": [{"text": "gzip ile eski loglar; PGPASSWORD=Canary-Env-22"}]},
               headers=h)
    assert r.status_code == 200 and r.json()["phase"] == "resolved" and r.json()["status"] == "done"
    assert "Canary-Env-22" not in r.json()["resolution"]
    assert [x["id"] for x in c.get("/api/act/inbox?tab=closed", headers=h).json() if x["kind"] == "case"] == [cid]
    t = c.post("/api/act/triggers", json={"source": "external", "key": "ext:1", "host": "web-prod-07", "title": "Dış tetik"}, headers=h)
    assert t.status_code == 201 and t.json()["action"] == "opened"
    assert c.post("/api/act/triggers", json={"source": "bogus", "key": "x"}, headers=h).json()["detail"]["code"] == "act_param_invalid"
    c.svc.users.register("view@corp.com", PW, name="v", role="viewer")
    v = login(c, "view@corp.com")
    assert c.get("/api/act/inbox", headers=v).status_code == 200
    assert c.post("/api/act/cases", json={"title": "x", "host": "h"}, headers=v).status_code == 403


def test_bizflow_stuck_becomes_a_case(c):
    act = on(c)
    c.svc.bizflows.check_stuck = lambda min_stuck: [{"flow_id": 3, "flow": "Sipariş", "step": 2, "step_name": "Ödeme", "stuck": 25,
                                                     "oldest_s": 4000, "hosts": ["pay-01"], "samples": ["k1"], "team_id": None}]
    assert act.check_bizflows() == 1
    act.drain()
    (case,) = cases(c)
    assert case["case_key"] == "bizflow:3:2" and case["family"] == "flow:3:2" and case["host"] == "pay-01" and "Ödeme" in case["title"]
    fid = c.svc.bizflows.save("Akış", r"(ORD-\d+)", [{"name": "a", "match": "a"}, {"name": "b", "match": "b"}])["id"]
    assert type(c.svc.bizflows).check_stuck(c.svc.bizflows, 20) == [] and fid
