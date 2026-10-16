"""Any time range on the operations screen: the last hour from the live counters, older ranges from the stored batches
(sampled when large) and the per-minute history; raw lines newest first with paging; team scope on every answer."""
import json
import os
from datetime import datetime, timedelta, timezone

from watchover.db import Database
from watchover.history import History
from watchover.knowledge import Knowledge
from watchover.live import LiveStore
from watchover.timerange import RangeView, parse_range, step_minutes

UTC = timezone.utc


def _feed(st, ts, host, n=50):
    lines = [json.dumps({"ts": ts.isoformat(), "level": "error" if j % 10 == 0 else "info", "service": "web", "env": "prod",
                         "msg": f"Timeout after {j} ms id={j}" if j % 10 == 0 else f"GET /x/{j} 200 {j * 3}ms"}) for j in range(n)]
    st.ingest("app.jsonl", ("\n".join(lines) + "\n").encode(), agent=host)


def _store(tmp_path, maxlen=100):
    st = LiveStore(spool=None, maxlen=maxlen)
    st.attach_db(Database(":memory:"))
    h = History(Knowledge(str(tmp_path / "k.db")))
    st.on_ingest = h.add
    return st, h


def test_parse_range_and_steps():
    s, e, live = parse_range(15)
    assert live and abs((e - s).total_seconds() - 900) < 1
    s, e, live = parse_range(None, "2026-01-02T10:00:00Z", "2026-01-02T09:00:00Z")      # reversed: swapped
    assert not live and s.hour == 9 and e.hour == 10
    assert [step_minutes(m) for m in (15, 60, 360, 1440, 10080)] == [1, 1, 5, 15, 120]


def test_live_past_and_history_sources(tmp_path):
    st, h = _store(tmp_path)
    now = datetime.now(UTC)
    for i in range(10):
        _feed(st, now - timedelta(hours=5) + timedelta(seconds=i), f"prd-web-0{i % 3 + 1}")
    for i in range(6):
        _feed(st, now - timedelta(minutes=3), f"prd-db-0{i % 2 + 1}")
    h.flush()
    rv = RangeView(st, h)
    x = rv.summary(*parse_range(15)[:2])
    assert x["source"] == "live" and x["total"] == 300 and x["errors"] == 30 and x["hosts"] == 2 and x["prev"]["total"] == 0
    s, e, live = parse_range(None, (now - timedelta(hours=5, minutes=5)).isoformat(), (now - timedelta(hours=4, minutes=50)).isoformat())
    x = rv.summary(s, e)
    assert not live and x["source"] == "history" and x["total"] == 500 and sum(b["total"] for b in x["series"]) == 500 and x["hosts"] == 3
    f = rv.fleet(s, e)
    assert f["source"] == "history" and [(p["pool"], p["events"], p["host_count"]) for p in f["pools"]] == [("prd-web", 500, 3)]
    g = rv.groups(s, e)
    assert g["source"] == "stored" and g["sample"] == 1 and g["total"] == 500 and [x["count"] for x in g["groups"]] == [450, 50]
    assert rv.groups(s, e, host=("prd-web-01",), min_sev="ERROR")["total"] == 20
    L = rv.logs(s, e, min_sev="ERROR", limit=20)
    assert len(L["lines"]) == 20 and L["next"] and all(l["severity"] == "ERROR" for l in L["lines"])
    older = rv.logs(s, e, min_sev="ERROR", limit=20, before=L["next"])
    assert older["lines"] and older["lines"][0]["ts"] < L["lines"][-1]["ts"] or older["lines"][0]["ts"] <= L["next"]
    assert rv.logs(s, e, q="timeout", host=("prd-web-02",), limit=500)["lines"] and all(l["host"] == "prd-web-02" for l in rv.logs(s, e, q="timeout", host=("prd-web-02",))["lines"])
    x = rv.summary(*parse_range(1440)[:2])
    assert x["source"] == "history" and x["total"] == 800 and x["step_min"] == 15


def test_sampled_past_range(tmp_path, monkeypatch):
    import watchover.timerange as tr
    monkeypatch.setattr(tr, "MAX_EVENTS", 200)
    st, h = _store(tmp_path)
    t0 = datetime.now(UTC) - timedelta(hours=3)
    for i in range(20):
        _feed(st, t0 + timedelta(seconds=i), "prd-web-01")                       # 1000 events in 20 batches
    rv = RangeView(st, h)
    g = rv.groups(t0 - timedelta(minutes=1), t0 + timedelta(minutes=2))
    assert g["sample"] == 5 and g["total"] == 1000


def test_range_api_and_scope(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("db@corp.com", "Str0ng!Passw0rd", role="operator")
    now = datetime.now(UTC)
    for i in range(6):
        _feed(svc.live, now - timedelta(minutes=2), f"prd-web-0{i % 3 + 1}" if i < 3 else f"prd-db-0{i % 3 + 1}")
    with TestClient(a) as c:
        login = lambda e, p: {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": e, "password": p}).json()["token"]}   # noqa: E731
        admin = login("admin@watchover.local", "Test-Yonetici-Parola-42")
        s = c.get("/api/live/range/summary?last=15", headers=admin).json()
        assert s["live"] and s["total"] == 300 and s["series"] and s["step_min"] == 1
        assert c.get("/api/live/range/summary?last=15&pool=prd-db", headers=admin).json()["total"] == 150
        f = c.get("/api/live/range/fleet?last=15", headers=admin).json()
        assert f["hosts"] == 6 and {p["pool"] for p in f["pools"]} == {"prd-web", "prd-db"}
        g = c.get("/api/live/range/groups?last=15&severity=ERROR", headers=admin).json()
        assert g["total"] == 30 and g["source"] == "live"
        L = c.get("/api/live/range/logs?last=15&severity=ERROR&limit=10", headers=admin).json()
        assert len(L["lines"]) == 10 and L["next"]
        st_ = (now - timedelta(minutes=5)).isoformat(); en = now.isoformat()
        assert c.get(f"/api/live/range/summary?start={st_}&end={en}", headers=admin).json()["live"] is True       # unescaped "+00:00"
        from urllib.parse import quote
        assert c.get(f"/api/live/range/summary?start={quote(st_)}&end={quote(en)}", headers=admin).json()["total"] == 300
        c.post("/api/teams", json={"name": "DB", "rules": [{"field": "host", "op": "glob", "value": "prd-db-*"}], "members": ["db@corp.com"]}, headers=admin)
        h = login("db@corp.com", "Str0ng!Passw0rd")
        assert c.get("/api/live/range/summary?last=15", headers=h).json()["total"] == 150
        assert {p["pool"] for p in c.get("/api/live/range/fleet?last=15", headers=h).json()["pools"]} == {"prd-db"}
        assert {l["host"][:6] for l in c.get("/api/live/range/logs?last=15", headers=h).json()["lines"]} == {"prd-db"}
        assert c.get("/api/live/range/groups?last=15&pool=prd-web", headers=h).json()["total"] == 0


def test_detail_services_and_fixed_range_report(tmp_path, monkeypatch):
    """The detail behind the headline numbers follows the range (services with errors and p95, errors by host, budget burn), and the
    SLO report of a fixed past range covers that range, not the last N minutes."""
    st, h = _store(tmp_path)
    now = datetime.now(UTC)
    for i in range(6):
        _feed(st, now - timedelta(minutes=2), f"prd-web-0{i % 3 + 1}")
    rv = RangeView(st, h)
    d = rv.detail(*parse_range(15)[:2])
    assert d["source"] == "live" and d["total"] == 300 and d["services"][0]["service"] == "web" and d["services"][0]["errors"] == 30
    assert d["services"][0]["hosts"] == 3 and d["services"][0]["p95_ms"] and d["by_service"] == [("web", 30)] and len(d["per_minute"]) == 16
    assert d["templates"] and d["recent"] and d["error_budget"] == 0.0
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k2.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for i in range(4):
        _feed(svc.live, now - timedelta(hours=3), f"prd-web-0{i % 2 + 1}")
    svc.history.flush()
    with TestClient(a) as c:
        hd = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Test-Yonetici-Parola-42"}).json()["token"]}
        from urllib.parse import quote
        s_, e_ = quote((now - timedelta(hours=3, minutes=5)).isoformat()), quote((now - timedelta(hours=2, minutes=55)).isoformat())
        d = c.get(f"/api/live/range/detail?start={s_}&end={e_}", headers=hd).json()
        assert d["source"] == "history" and d["total"] == 200 and d["services"][0]["lines"] == 200 and d["live"] is False
        r = c.get(f"/api/live/report?start={s_}&end={e_}", headers=hd)
        assert r.status_code == 200 and "200" in r.text.replace(".", "").replace(",", "")
        assert c.get("/api/live/report?window=15", headers=hd).status_code == 200
