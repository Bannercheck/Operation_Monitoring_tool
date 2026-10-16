"""Built-in dashboards' aggregates (watchover.dash, /api/dash/*): infrastructure over a range at 300 servers, recent alarms across
threshold states, anomalies and the alert log, lines per pool; each bounded, cached per scope and narrowed to the user's team."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc
ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.api.routers import dash as dash_router
    dash_router.CACHE._d.clear()
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("opa@corp.com", PW, name="Opa", role="viewer")
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def metrics(svc, hosts, minutes=10, every_s=30):
    """Readings for `hosts` over the last `minutes`: cpu rises with the host's index, memory flat, disk by index."""
    now = datetime.now(UTC)
    rows = []
    for k in range(int(minutes * 60 / every_s)):
        ts = now - timedelta(seconds=minutes * 60 - k * every_s)
        for i, h in enumerate(hosts):
            env = "prod" if i % 3 else "test"
            rows += [(ts, h, "cpu", 20.0 + (i * 80.0 / max(1, len(hosts) - 1)), env), (ts, h, "memory", 50.0, env), (ts, h, "disk", 30.0 + i % 60, env)]
    with svc.live.lock:
        svc.live.metrics.extend(rows)
    return rows


def lines(svc, host, n, err=0):
    now = datetime.now(UTC).isoformat()
    body = "\n".join(json.dumps({"ts": now, "level": "error" if i < err else "info", "service": "svc", "host": host, "msg": f"line {i}"}) for i in range(n))
    svc.live.ingest("app.jsonl", (body + "\n").encode(), "test")


def test_infra_at_300_hosts_is_bounded_and_fast(app):
    svc = app.svc; admin = login(app, *ADMIN)
    hosts = [f"srv-{i:03d}" for i in range(300)]
    metrics(svc, hosts)
    r = app.get("/api/dash/infra?last=15&top=10&series_top=6", headers=admin)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["hosts"] == 300 and d["step_min"] == 1
    cpu = d["fleet"]["cpu"]
    assert cpu["hosts"] == 300 and cpu["worst"] == "srv-299" and cpu["max"] == 100.0 and 59 < cpu["avg"] < 61
    assert cpu["crit"] == 85.0 and cpu["warn"] == 70.0 and cpu["crit_n"] > 0 and cpu["warn_n"] > 0
    top = d["top"]["cpu"]
    assert len(top) == 10 and top[0]["host"] == "srv-299" and top[0]["level"] == "crit" and [x["value"] for x in top] == sorted((x["value"] for x in top), reverse=True)
    assert {"min", "avg", "max", "env", "pool"} <= set(top[0])
    s = d["series"]["cpu"]
    assert len(s["hosts"]) == 6 and s["hosts"] == sorted(s["hosts"]) and len(s["rows"]) == 15
    assert any(row[s["hosts"][0]] is not None for row in s["rows"])
    assert len(d["trend"]) == 15 and all(set(row) >= {"t", "cpu", "memory", "disk", "gpu"} for row in d["trend"])
    assert d["fleet"]["gpu"]["avg"] is None and d["top"]["gpu"] == []
    # one environment only
    t = app.get("/api/dash/infra?last=15&env=test", headers=admin).json()
    assert t["hosts"] == 100
    # one server, several servers, a pool: the gauges, tables and lines cover exactly those servers
    one = app.get("/api/dash/infra?last=15&host=srv-007", headers=admin).json()
    assert one["hosts"] == 1 and one["fleet"]["cpu"]["worst"] == "srv-007" and [x["host"] for x in one["top"]["cpu"]] == ["srv-007"]
    two = app.get("/api/dash/infra?last=15&host=srv-007,srv-299", headers=admin).json()
    assert two["hosts"] == 2 and {x["host"] for x in two["top"]["cpu"]} == {"srv-007", "srv-299"} and set(two["series"]["cpu"]["hosts"]) == {"srv-007", "srv-299"}
    both = app.get("/api/dash/infra?last=15&env=test&host=srv-007", headers=admin).json()   # srv-007 is prod (7 % 3 != 0): nothing left
    assert both["hosts"] == 0
    assert app.get("/api/dash/infra?last=15&pool=srv", headers=admin).json()["hosts"] == 300
    assert app.get("/api/dash/infra?last=15&pool=nope", headers=admin).json()["hosts"] == 0
    # uncached: the computation itself, at 300 servers x 3 metrics x 20 readings, well inside the budget
    from watchover.api.routers import dash as dash_router
    took = []
    for _ in range(8):
        dash_router.CACHE._d.clear()
        t0 = time.perf_counter(); app.get("/api/dash/infra?last=15", headers=admin); took.append((time.perf_counter() - t0) * 1000)
    took.sort()
    assert took[len(took) // 2] < 600, took        # p95 under 150 ms on the demo machine; generous here for slow CI runners
    t0 = time.perf_counter(); app.get("/api/dash/infra?last=15", headers=admin); cached = (time.perf_counter() - t0) * 1000
    assert cached < took[0] + 50


def test_infra_and_pools_follow_the_team(app):
    svc = app.svc; admin = login(app, *ADMIN)
    metrics(svc, ["a-app-01", "a-app-02", "b-web-01"], minutes=3)
    for h, n, err in (("a-app-01", 30, 3), ("a-app-02", 10, 0), ("b-web-01", 50, 10), ("a-db-01", 5, 5)):
        lines(svc, h, n, err)
    ta = app.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    rid = next(r["id"] for r in ta["roles"] if r["key"] == "operator")
    assert app.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": rid}, headers=admin).status_code == 200
    svc.users.set_role(svc.users.get("opa@corp.com")["id"], "member", svc.roles.names())
    svc.teams.invalidate()
    opa = login(app, "opa@corp.com")
    d = app.get("/api/dash/infra?last=15", headers=opa).json()
    assert d["hosts"] == 2 and {x["host"] for x in d["top"]["cpu"]} == {"a-app-01", "a-app-02"}
    # asking for the other team's host gives nothing, not their data
    assert app.get("/api/dash/infra?last=15&host=b-web-01", headers=opa).json()["hosts"] == 0
    p = app.get("/api/dash/pools?last=15", headers=opa).json()
    assert {x["pool"] for x in p["pools"]} == {"a-app", "a-db"}
    app_row = next(x for x in p["pools"] if x["pool"] == "a-app")
    assert app_row["lines"] == 40 and app_row["errors"] == 3
    assert sum(row.get("a-app", 0) for row in p["series"]) == 40
    # the administrator sees every pool; top=1 folds the rest into one "other" series
    pa = app.get("/api/dash/pools?last=15&top=1", headers=admin).json()
    assert [x["pool"] for x in pa["pools"]] == ["b-web", "~other"] and pa["pools"][1]["pools"] == 2 and pa["names"] == ["b-web", "~other"]
    assert sum(row["~other"] for row in pa["series"]) == 45


def test_alarms_merge_sources_by_permission_and_scope(app):
    svc = app.svc; admin = login(app, *ADMIN)
    now = datetime.now(UTC)
    iso = lambda m: (now - timedelta(minutes=m)).isoformat(timespec="seconds")  # noqa: E731
    for host, metric, level, ago in (("a-app-01", "cpu", "crit", 5), ("b-web-01", "disk", "warn", 8)):
        svc.kb._exec("INSERT INTO threshold_state (host, metric, level, since, value, limit_value, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (host, metric, level, iso(ago), 97.0, 85.0, iso(0)))
    svc.kb._exec("INSERT INTO anomalies (kind, key, env, host, service, title, score, first_seen, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("volume", "v:a-app-01", "prod", "a-app-01", "svc", "Log volume x4", 9.5, iso(12), iso(2), "open"))
    svc.kb._exec("INSERT INTO anomalies (kind, key, env, host, service, title, score, first_seen, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("volume", "v:old", "prod", "a-old-01", "svc", "Long ago", 5.0, iso(600), iso(500), "resolved"))
    for key, sev, title, ago in (("thr:a-app-01:cpu:crit", "critical", "cpu a-app-01", 4), ("query:7", "medium", "Payment errors: 12 lines / 5 min", 3),
                                 ("slo:all", "high", "Availability below SLO", 1), ("errors:all", "low", "old", 300)):
        svc.kb._exec("INSERT INTO alerts (ts, rule_id, key, severity, title, body, channels, recipients, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (iso(ago), 1, key, sev, title, "", "email", "", 1))
    d = app.get("/api/dash/alarms?last=60", headers=admin).json()
    kinds = [i["kind"] for i in d["items"]]
    assert d["total"] == 5 and d["by_kind"] == {"threshold": 2, "anomaly": 1, "alert": 1, "log": 1}
    assert "thr:a-app-01:cpu" in {i["id"] for i in d["items"]} and not any(i["id"].startswith("al:") and i["kind"] == "threshold" for i in d["items"])   # the open state stands for its alert
    assert d["by_sev"] == {"crit": 3, "warn": 2, "info": 0}
    assert [i["ts"] for i in d["items"]] == sorted((i["ts"] for i in d["items"]), reverse=True) and kinds[0] == "alert"
    assert app.get("/api/dash/alarms?last=60&limit=2", headers=admin).json()["total"] == 5
    assert len(app.get("/api/dash/alarms?last=60&limit=2", headers=admin).json()["items"]) == 2
    # one server: its own state and anomaly; alerts with no server drop out
    one = app.get("/api/dash/alarms?last=60&host=a-app-01", headers=admin).json()
    assert {i["kind"] for i in one["items"]} == {"threshold", "anomaly"} and all(i["host"] == "a-app-01" for i in one["items"])
    # a team operator: their team's servers only, and the alert log needs its own permission
    lines(svc, "a-app-01", 1); lines(svc, "b-web-01", 1)
    # an environment variable: the servers of that environment only (live.hosts() maps host -> environment name)
    ev = svc.live.hosts()["a-app-01"]
    r = app.get(f"/api/dash/alarms?last=60&env={ev}", headers=admin)
    assert r.status_code == 200, r.text
    assert {i["host"] for i in r.json()["items"] if i["kind"] == "threshold"} == {"a-app-01", "b-web-01"}
    assert not [i for i in app.get("/api/dash/alarms?last=60&env=nowhere", headers=admin).json()["items"] if i["kind"] == "threshold"]
    ta = app.post("/api/teams", json={"name": "A", "rules": [{"field": "host", "op": "glob", "value": "a-*"}]}, headers=admin).json()
    rid = next(r["id"] for r in ta["roles"] if r["key"] == "operator")
    app.put(f"/api/teams/{ta['id']}/members/opa@corp.com", json={"role_id": rid}, headers=admin)
    svc.users.set_role(svc.users.get("opa@corp.com")["id"], "member", svc.roles.names())
    svc.teams.invalidate()
    opa = login(app, "opa@corp.com")
    r = app.get("/api/dash/alarms?last=60", headers=opa)
    assert r.status_code == 200, r.text
    hosts = {i["host"] for i in r.json()["items"]}
    assert "b-web-01" not in hosts
    assert not any(i["kind"] in ("alert", "log") for i in r.json()["items"])     # installation-wide alerts are not a team's


def test_dash_needs_sign_in(app):
    for p in ("infra", "alarms", "pools"):
        assert app.get(f"/api/dash/{p}?last=15").status_code == 401
