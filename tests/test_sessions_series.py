"""Client sessions, latency percentiles and top-host series on the operations screen: sessions named by the lines' attributes
(each key family), pseudonymised at ingest so no raw id / user / IP ever comes back, per host and per bar, capped per host and
5-minute bucket, team scope; p50 / p90 / p99 per bar; top hosts per metric; a stored past range (sampled) and a history-only one."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import watchover.rollup as ro
from watchover.db import Database
from watchover.history import History
from watchover.knowledge import Knowledge
from watchover.live import LiveStore
from watchover.models import Observation
from watchover.privacy import pseudonym
from watchover.rollup import Rollup, session_link, session_of
from watchover.timerange import RangeView, parse_range

UTC = timezone.utc
RAW = ["JSX-ZEYNEP-77", "ayse.yilmaz@corp.com", "10.20.30.40", "KC-SESS-991", "mehmet.k", "172.16.99.7", "PHP-QQQ-1"]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "sessions-test")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)


def _rec(ts, host, msg="ok", level="info", service="web", **attrs):
    return {"ts": ts.isoformat(), "level": level, "service": service, "host": host, "env": "prod", "msg": msg, **attrs}


def _ingest(st, recs, agent="agent-1"):
    st.ingest("app.jsonl", ("\n".join(json.dumps(r) for r in recs) + "\n").encode(), agent=agent)


def _store(tmp_path, maxlen=100):
    st = LiveStore(spool=None, maxlen=maxlen)
    st.attach_db(Database(":memory:"))
    h = History(Knowledge(str(tmp_path / "k.db")))
    st.on_ingest = h.add
    return st, h


def _obs(ts, host, msg, sev="INFO", service="web", **attrs):
    return Observation(timestamp=ts, message=msg, severity=sev, service=service, host=host, environment="prod", attributes=dict(attrs))


def test_session_key_families():
    for k in ("session", "session_id", "SessionId", "JSESSIONID", "session.id", "sid", "PHPSESSID", "ASP.NET_SessionId"):
        x = session_of({"agent": "a", k: "abc-1"})
        assert x[:2] == (pseudonym("abc-1", "s"), "app") and x[2] == x[3] == "", k
    assert session_of({"session_state": "kc1"})[1] == "sso"
    assert session_of({"sessionId": "kc2", "clientId": "hr-portal", "userId": "u-1"})[:3] == (pseudonym("kc2", "s"), "sso", pseudonym("u-1"))
    assert session_of({"session_id": "kc3", "client_id": "hr"})[1] == "sso"
    assert session_of({"sessionId": "a1", "session_state": "b2"})[0] == pseudonym("a1", "s")           # the session id wins over session_state
    for k in ("user", "user_id", "userid", "username", "user.name", "uid", "cs-username", "usr", "USER"):
        x = session_of({k: "ayse"})
        assert x[1] == "client" and x[2] == pseudonym("ayse") and x[3] == "", k
    a, b = session_of({"username": "ayse", "client_ip": "10.0.0.1"}), session_of({"username": "ayse", "client_ip": "10.0.0.2"})
    assert a[0] != b[0] and a[2] == b[2] and a[3] == pseudonym("10.0.0.1", "c")                            # user + client address
    for k in ("client_ip", "clientip", "remote_addr", "remote_ip", "c-ip", "src_ip", "source_ip", "client.ip", "x_forwarded_for", "X-Forwarded-For"):
        x = session_of({k: "10.1.1.1"})
        assert x[1] == "client" and x[3] == pseudonym("10.1.1.1", "c") and x[0].startswith("s_") and x[0][2:] != x[3][2:], k
    assert session_of({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})[3] == pseudonym("1.2.3.4", "c")              # the first address is the client
    x = session_of({"user": "ayse", "jsessionid": "J1", "remote_addr": "10.0.0.9"})
    assert x[:4] == (pseudonym("J1", "s"), "app", pseudonym("ayse"), pseudonym("10.0.0.9", "c"))         # a session id wins, user / client ride along
    x = session_of({"rum.session": "r4nd0m", "rum.user_ref": "u_0123456789abcdef", "rum.kind": "error"})
    assert x[1:5] == ("rum", "u_0123456789abcdef", "", "r4nd0m") and session_link(x, 0) == "/rum/sessions/r4nd0m"
    for a in ({"agent": "x"}, {"trace_id": "abc"}, {"user": "-"}, {"user": "anonymous"}, {"session_id": ""}):
        assert session_of(a) is None, a
    assert session_of({"session_id": "a", "duration_ms": 5})[5] is True and session_of({"session_id": "a"})[5] is False
    assert session_link(session_of({"session_id": "a", "duration_ms": 5}), 0).startswith("/traces?session=s_")
    assert session_link(session_of({"session_id": "a"}), 0) == "" and session_link(session_of({"session_id": "a"}), ro.TRACED).startswith("/traces?session=s_")
    assert session_link(session_of({"user": "a"}), ro.TRACED) == ""


def test_live_sessions_per_host_series_and_privacy(tmp_path):
    st, _h = _store(tmp_path, maxlen=10_000)
    now = datetime.now(UTC)
    t5, t2 = now - timedelta(minutes=5), now - timedelta(minutes=2)
    _ingest(st, [_rec(t5, "prd-web-01", "GET /a 200 12 ms", jsessionid="JSX-ZEYNEP-77", user="ayse.yilmaz@corp.com", client_ip="10.20.30.40"),
                 _rec(t5, "prd-web-01", "login", jsessionid="PHP-QQQ-1", username="mehmet.k"),
                 _rec(t2, "prd-web-01", "pay failed", level="error", jsessionid="PHP-QQQ-1", username="mehmet.k"),
                 _rec(t2, "prd-web-01", "timeout", level="error", service="pay", jsessionid="JSX-ZEYNEP-77", user="ayse.yilmaz@corp.com"),
                 _rec(t2, "prd-web-02", "GET /b 200", remote_addr="172.16.99.7"),
                 _rec(t2, "prd-sso-01", "LOGIN", type="LOGIN", clientId="hr", sessionId="KC-SESS-991", userId="ayse.yilmaz@corp.com"),
                 _rec(t2, "prd-web-02", "no session here")])
    rv = RangeView(st)
    x = rv.sessions(*parse_range(15)[:2])
    assert x["source"] == "live" and x["step_min"] == 1 and x["total"] == 4 and x["overflow"] == 0
    assert [(r["host"], r["sessions"], r["users"], r["errors"]) for r in x["by_host"]] == [("prd-web-01", 2, 2, 2), ("prd-sso-01", 1, 1, 0), ("prd-web-02", 1, 0, 0)]
    act = {b["t"]: b["n"] for b in x["active_series"]}
    assert len(x["active_series"]) == 16 and act[int(t5.timestamp() // 60) * 60] == 2 and act[int(t2.timestamp() // 60) * 60] == 4 and sum(act.values()) == 6
    by = {r["id"]: r for r in x["sessions"]}
    z = by[pseudonym("JSX-ZEYNEP-77", "s")]
    assert z["kind"] == "app" and z["user"] == pseudonym("ayse.yilmaz@corp.com") and z["client"] == pseudonym("10.20.30.40", "c")
    assert z["lines"] == 2 and z["errors"] == 1 and z["services"] == ["pay", "web"] and z["hosts"] == ["prd-web-01"] and 170 <= z["duration_s"] <= 190
    assert z["link"] == f"/traces?session={z['id']}"                                                      # it had a duration: it is a span
    assert by[pseudonym("PHP-QQQ-1", "s")]["link"] == ""
    assert by[pseudonym("KC-SESS-991", "s")]["kind"] == "sso" and by[pseudonym("KC-SESS-991", "s")]["user"] == z["user"]   # same person, same token
    assert [r["kind"] for r in x["sessions"]][:2] == ["app", "app"] and x["sessions"][0]["errors"] == 1          # newest first, errors first on ties
    assert rv.sessions(*parse_range(15)[:2], host=("prd-web-02",))["total"] == 1
    assert [r["id"] for r in rv.sessions(*parse_range(15)[:2], q="pay")["sessions"]] == [z["id"]]
    assert rv.sessions(*parse_range(15)[:2], limit=1)["sessions"].__len__() == 1
    text = json.dumps([x, rv.hostseries(*parse_range(15)[:2], metric="sessions"), rv.latency(*parse_range(15)[:2])])
    for raw in RAW:
        assert raw.lower() not in text.lower(), raw
    from watchover.logstore import row_bytes
    for row in st.db._exec("SELECT payload, pz FROM live_batches"):                                     # stored batches carry the pseudonyms only
        text = row_bytes(row).decode()                                                                 # (v2 rows: compressed, with raw + attributes)
        stored = [json.loads(x) for x in text.split("\n") if x]
        assert '"ses"' in text and not any(r in json.dumps([{k: v for k, v in d.items() if k != "msg"} for d in stored]) for r in RAW)
        assert not any(r in text for r in RAW[:4] + RAW[5:])


def test_overflow_cap(monkeypatch):
    monkeypatch.setattr(ro, "SES_CAP", 3)
    r = Rollup()
    ts = datetime.now(UTC) - timedelta(minutes=2)
    r.add([_obs(ts, "prd-web-01", "x", session_id=f"S{i}") for i in range(5)] + [_obs(ts, "prd-web-01", "x", session_id="S0")] +
          [_obs(ts, "prd-web-02", "x", session_id="S9")])
    x = r.sessions(15)
    assert x["total"] == 4 and x["overflow"] == 2 and {h["host"]: h["sessions"] for h in x["by_host"]} == {"prd-web-01": 3, "prd-web-02": 1}
    assert next(s for s in x["sessions"] if s["id"] == pseudonym("S0", "s"))["lines"] == 2
    assert r.sessions(15, host="prd-web-02")["overflow"] == 0
    monkeypatch.setattr(ro, "SES_BUCKET_CAP", 5)                                                    # and over all hosts of a bucket
    monkeypatch.setattr(ro, "OVER_CAP", 4)
    r = Rollup()
    r.add([_obs(ts, f"prd-app-{i:02d}", "x", session_id=f"B{i}") for i in range(12)])
    x = r.sessions(15)
    assert x["total"] == 5 and x["overflow"] == 4                                                       # 3 more only counted as a floor


def test_latency_percentiles_per_bar():
    r = Rollup(keep=None)
    m0 = int(datetime(2026, 10, 1, 10, 0, tzinfo=UTC).timestamp() // 60)
    t = lambda m, s=0: datetime.fromtimestamp(m * 60 + s, UTC)   # noqa: E731
    r.add([_obs(t(m0, i % 50), "a", f"GET /x took {i} ms") for i in range(1, 101)])
    r.add([_obs(t(m0 + 2), "b", "slow 1000 ms") for _ in range(10)] + [_obs(t(m0 + 2), "b", "no duration")])
    s = r.latency_series((m0, m0 + 2))
    assert [x["n"] for x in s] == [100, 0, 10]
    assert (s[0]["p50"], s[0]["p90"], s[0]["p99"]) == (50.0, 90.0, 99.0)
    assert s[1]["p50"] is None and s[2]["p50"] == 875.0
    assert [x["n"] for x in r.latency_series((m0, m0 + 2), host="b")] == [0, 0, 10]
    s2 = r.latency_series((m0, m0 + 3), step=2)
    assert [x["t"] for x in s2] == [m0 * 60, (m0 + 2) * 60] and [x["n"] for x in s2] == [100, 10]


def test_hostseries_ranking_per_metric():
    r = Rollup(keep=None)
    m0 = int(datetime(2026, 10, 1, 10, 0, tzinfo=UTC).timestamp() // 60)
    t = datetime.fromtimestamp(m0 * 60 + 5, UTC)
    t1 = datetime.fromtimestamp(m0 * 60 + 65, UTC)
    ev = [_obs(t, "h-a", "ok 10 ms", "ERROR" if i < 5 else "INFO", user="u1") for i in range(100)]
    ev += [_obs(t1, "h-b", "slow 500 ms", "ERROR" if i < 20 else "INFO", user=f"b{i % 2}") for i in range(50)]
    ev += [_obs(t, "h-c", "very slow 2000 ms") for _ in range(10)]
    ev += [_obs(t1, "h-d", "plain", "ERROR" if i == 0 else "INFO", session_id=f"d{i % 4}") for i in range(5)]
    r.add(ev)
    hs = lambda metric, top=6: r.host_series((m0, m0 + 1), metric, top=top)   # noqa: E731
    x = hs("lines", 2)
    assert x["hosts"] == [{"host": "h-a", "total": 100}, {"host": "h-b", "total": 50}]
    assert x["series"] == [{"t": m0 * 60, "h-a": 100, "h-b": 0}, {"t": (m0 + 1) * 60, "h-a": 0, "h-b": 50}]
    assert [h["host"] for h in hs("errors")["hosts"]] == ["h-b", "h-a", "h-d"]
    er = hs("error_rate")
    assert [(h["host"], h["total"]) for h in er["hosts"]] == [("h-b", 0.4), ("h-a", 0.05), ("h-d", 0.2)]
    assert er["series"][0]["h-b"] is None and er["series"][1]["h-b"] == 0.4
    p = hs("p95")
    assert [h["host"] for h in p["hosts"]] == ["h-c", "h-b", "h-a"] and p["hosts"][0]["total"] == 1975.0 and p["series"][1]["h-a"] is None
    s = hs("sessions")
    assert [(h["host"], h["total"]) for h in s["hosts"]] == [("h-d", 4), ("h-b", 2), ("h-a", 1)]
    assert r.host_series((m0, m0 + 1), "lines", host=("h-c", "h-d"))["hosts"] == [{"host": "h-c", "total": 10}, {"host": "h-d", "total": 5}]


def test_stored_past_range_sampled(tmp_path, monkeypatch):
    st, h = _store(tmp_path)
    t0 = datetime.now(UTC) - timedelta(hours=3)
    for i in range(20):                                                     # 20 batches x 50 lines: 10 sessions on two hosts
        _ingest(st, [_rec(t0 + timedelta(seconds=i), f"prd-app-0{j % 2 + 1}", f"step {j} {j} ms", "error" if j == 0 else "info",
                          session_id=f"JSX-{j % 10}", user=f"person{j % 5}") for j in range(50)])
    for i in range(4):
        _ingest(st, [_rec(datetime.now(UTC) - timedelta(minutes=1), "prd-app-01", "recent") for _ in range(50)])
    rv = RangeView(st, h)
    s, e = t0 - timedelta(minutes=1), t0 + timedelta(minutes=2)
    x = rv.sessions(s, e)
    assert x["source"] == "stored" and x["sample"] == 1 and x["total"] == 10 and sum(r["lines"] for r in x["sessions"]) == 1000
    assert {r["id"] for r in x["sessions"]} == {pseudonym(f"JSX-{j}", "s") for j in range(10)} and x["sessions"][0]["errors"] == 20
    assert [(b["host"], b["sessions"], b["users"]) for b in x["by_host"]] == [("prd-app-01", 5, 5), ("prd-app-02", 5, 5)]
    assert rv.hostseries(s, e, metric="sessions")["hosts"] == [{"host": "prd-app-01", "total": 5}, {"host": "prd-app-02", "total": 5}]
    lat = rv.latency(s, e)
    assert lat["source"] == "stored" and sum(b["n"] for b in lat["series"]) == 1000
    monkeypatch.setattr("watchover.timerange.MAX_EVENTS", 200)
    rv = RangeView(st, h)
    x = rv.sessions(s, e)
    assert x["sample"] == 5 and sum(r["lines"] for r in x["sessions"]) == 1000 and x["total"] == 10    # every 5th batch, counts scaled back
    hs = rv.hostseries(s, e, metric="lines")
    assert hs["source"] == "stored" and sum(r["total"] for r in hs["hosts"]) == 1000 and sum(v for row in hs["series"] for k, v in row.items() if k != "t") == 1000
    assert "JSX-" not in json.dumps(x) and "person" not in json.dumps(x)


def test_history_only_range(tmp_path):
    st, h = _store(tmp_path)
    old = datetime.now(UTC) - timedelta(days=3)
    _ingest(st, [_rec(old, "prd-web-01", "GET /a 200 120 ms", "error" if j < 5 else "info", session_id=f"S{j}") for j in range(50)])
    _ingest(st, [_rec(old, "prd-web-02", "GET /b 200 3000 ms", session_id="S99") for _ in range(20)])
    for i in range(4):
        _ingest(st, [_rec(datetime.now(UTC) - timedelta(minutes=1), "prd-web-01", "recent") for _ in range(50)])
    h.flush()
    assert st.prune() == 0          # stamped 3 days ago but received now: kept 48 h after arrival (log detail), never listed for a range
    rv = RangeView(st, h)
    s, e = old - timedelta(minutes=2), old + timedelta(minutes=3)
    x = rv.sessions(s, e)
    assert x["source"] == "history" and x["total"] == 0 and x["sessions"] == [] and x["active_series"] == [] and x["by_host"] == []
    lat = rv.latency(s, e)
    assert lat["source"] == "history" and sum(b["n"] for b in lat["series"]) == 70
    bar = next(b for b in lat["series"] if b["n"])
    assert bar["p50"] == 170.0 and bar["p99"] > 2000                         # 50 x (100, 200] and 20 x (2000, 5000] ms (history buckets)
    hs = rv.hostseries(s, e, metric="lines")
    assert hs["source"] == "history" and hs["hosts"] == [{"host": "prd-web-01", "total": 50}, {"host": "prd-web-02", "total": 20}]
    assert sum(row["prd-web-01"] for row in hs["series"]) == 50
    assert [x["host"] for x in rv.hostseries(s, e, metric="p95")["hosts"]] == ["prd-web-02", "prd-web-01"]
    assert rv.hostseries(s, e, metric="error_rate")["hosts"] == [{"host": "prd-web-01", "total": 0.1}]
    assert rv.hostseries(s, e, metric="sessions")["hosts"] == []
    assert rv.hostseries(s, e, metric="lines", host=("prd-web-02",))["hosts"] == [{"host": "prd-web-02", "total": 20}]
    q = rv.sessions(datetime.now(UTC) - timedelta(hours=5), datetime.now(UTC) - timedelta(hours=4))
    assert q["source"] == "stored" and q["total"] == 0                                                 # inside the 48 h, just quiet


def test_trace_search_resolves_session_pseudonym(tmp_path):
    from watchover.traces import TraceStore
    tr = TraceStore(Database(str(tmp_path / "t.db")), settings=lambda: {"trace_sample_pct": 100})
    now = datetime.now(UTC)
    tr.add_events([_obs(now, "prd-web-01", "GET /a 200", session_id="JSX-ZEYNEP-77", duration_ms=120),
                   _obs(now, "prd-sso-01", "LOGIN", type="LOGIN", clientId="hr", sessionId="KC-SESS-991", userId="x")], keep_all=True)
    tok = session_of({"session_id": "JSX-ZEYNEP-77"})[0]
    hits = tr.search(session=tok, minutes=60)
    assert len(hits) == 1 and hits[0]["session_id"] == "JSX-ZEYNEP-77"
    assert len(tr.search(session=session_of({"sessionId": "KC-SESS-991", "clientId": "hr"})[0], minutes=60)) == 1
    assert tr.search(session="s_0000000000000000", minutes=60) == []
    assert len(tr.search(session="JSX-ZEYNEP-77", minutes=60)) == 1                                   # a raw id still works as before


def test_forged_stored_session_is_ignored(tmp_path):
    """Only the stored batches' own copy may skip pseudonymisation: a line that logs "_ses" itself is an ordinary line."""
    st, _h = _store(tmp_path)
    now = datetime.now(UTC)
    _ingest(st, [_rec(now, "prd-web-01", _ses=["ayse.yilmaz@corp.com", "app", "mehmet.k", "10.20.30.40", "", 0]), _rec(now, "prd-web-01", _ses="ab")])
    x = st.rollup.sessions(15)
    assert x["total"] == 0 and "ayse" not in json.dumps(x)
    assert session_of({"_ses": ["a", "app", "", "", "", 0], "user": "ayse"})[2] == pseudonym("ayse")


def test_access_log_client_and_trace_keys(tmp_path):
    """nginx / Apache / ALB lines name the remote address "client" (access parser): a client session; an application name there is not."""
    from watchover.traces import TraceStore
    x = session_of({"client": "10.20.30.40", "request": "GET / HTTP/1.1"})
    assert x[1] == "client" and x[3] == pseudonym("10.20.30.40", "c")
    assert session_of({"client": "2001:db8::7"})[3] == pseudonym("2001:db8::7", "c")
    assert session_of({"client": "hr-portal", "type": "LOGIN"}) is None
    tr = TraceStore(Database(str(tmp_path / "t.db")), settings=lambda: {"trace_sample_pct": 100})
    now = datetime.now(UTC)
    tr.add_events([_obs(now, "prd-web-01", "GET /a 200", PHPSESSID="PHP-QQQ-1", duration_ms=80)], keep_all=True)
    assert len(tr.search(session=session_of({"PHPSESSID": "PHP-QQQ-1"})[0], minutes=60)) == 1        # every key the screen links by is searchable


def test_range_api_team_scope_and_privacy(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("db@corp.com", "Str0ng!Passw0rd", role="operator")
    now = datetime.now(UTC) - timedelta(minutes=2)
    _ingest(svc.live, [_rec(now, "prd-web-01", "GET /a 200 40 ms", jsessionid="JSX-ZEYNEP-77", user="ayse.yilmaz@corp.com", client_ip="10.20.30.40"),
                       _rec(now, "prd-web-02", "GET /b 200 60 ms", level="error", remote_addr="172.16.99.7", username="mehmet.k"),
                       _rec(now, "prd-db-01", "query 900 ms", session_id="PHP-QQQ-1", user="dba1"),
                       _rec(now, "prd-db-02", "query 300 ms", sid="KC-SESS-991")])
    with TestClient(a) as c:
        login = lambda e, p: {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": e, "password": p}).json()["token"]}   # noqa: E731
        admin = login("admin@watchover.local", "Test-Yonetici-Parola-42")
        x = c.get("/api/live/range/sessions?last=15", headers=admin).json()
        assert x["source"] == "live" and x["total"] == 4 and len(x["sessions"]) == 4 and isinstance(x["rum"], dict)
        assert c.get("/api/live/range/sessions?last=15&pool=prd-db", headers=admin).json()["rum"] is None
        assert c.get("/api/live/range/sessions?last=15&pool=prd-db", headers=admin).json()["total"] == 2
        lat = c.get("/api/live/range/latency?last=15", headers=admin).json()
        assert lat["source"] == "live" and sum(b["n"] for b in lat["series"]) == 4 and {"t", "p50", "p90", "p99", "n"} <= set(lat["series"][0])
        hs = c.get("/api/live/range/hostseries?last=15&metric=p95&top=2", headers=admin).json()
        assert hs["metric"] == "p95" and [h["host"] for h in hs["hosts"]] == ["prd-db-01", "prd-db-02"] and len(hs["series"]) == 16
        assert c.get("/api/live/range/hostseries?last=15&metric=bogus", headers=admin).status_code == 422
        text = json.dumps([x, lat, hs, c.get("/api/live/range/hostseries?last=15&metric=sessions", headers=admin).json()])
        for raw in RAW:
            assert raw.lower() not in text.lower(), raw
        c.post("/api/teams", json={"name": "DB", "rules": [{"field": "host", "op": "glob", "value": "prd-db-*"}], "members": ["db@corp.com"]}, headers=admin)
        hd = login("db@corp.com", "Str0ng!Passw0rd")
        y = c.get("/api/live/range/sessions?last=15", headers=hd).json()
        assert y["total"] == 2 and y["rum"] is None and {b["host"] for b in y["by_host"]} == {"prd-db-01", "prd-db-02"}
        assert all(h.startswith("prd-db") for s in y["sessions"] for h in s["hosts"])
        for m in ("lines", "errors", "error_rate", "p95", "sessions"):
            got = c.get(f"/api/live/range/hostseries?last=15&metric={m}", headers=hd).json()
            assert all(h["host"].startswith("prd-db") for h in got["hosts"]) and all(k == "t" or k.startswith("prd-db") for row in got["series"] for k in row), m
        assert c.get("/api/live/range/sessions?last=15&pool=prd-web", headers=hd).json()["total"] == 0
        assert sum(b["n"] for b in c.get("/api/live/range/latency?last=15", headers=hd).json()["series"]) == 2
