"""Traces without instrumentation: span extraction from many log shapes, graded links, rollups (RED, percentiles, Apdex),
deterministic sampling, the waterfall's parent inference, the privacy layer and the APM API."""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from watchover.models import Observation
from watchover.pipeline import ingest_bytes
from watchover.privacy import path_template, pseudonym, scrub
from watchover.traces import BOUNDS, TraceStore, apdex, bucket, quantile, span_of, store_for_observations, waterfall

UTC = timezone.utc


def obs(attrs=None, msg="", svc="svc", sev="INFO", parser="jsonl", ts=None):
    return Observation(timestamp=ts or datetime.now(UTC), message=msg, service=svc, severity=sev, attributes=attrs or {}, parser=parser, source="f", line_no=1)


def test_access_logs_carry_duration_and_ids():
    nginx = '10.0.0.5 - - [14/Oct/2026:10:00:00 +0000] "GET /api/orders/123?x=1 HTTP/1.1" 200 512 "-" "curl/8" 0.182 0.170 req_id=abc123\n'
    o = ingest_bytes("access.log", nginx.encode())[0][0]
    sp = span_of(o)
    assert sp["duration_ms"] == 182.0 and sp["trace_id"] == "abc123" and sp["operation"] == "GET /api/orders/{id}" and sp["kind"] == "server"
    assert sp["attrs"]["upstream_ms"] == 170.0 and sp["link"] == "explicit"
    envoy = '[2026-10-14T10:00:00.123Z] "GET /api/v1/users/42 HTTP/1.1" 503 UF 0 1234 37 35 "10.0.0.1" "curl/8" "9a1b2c3d-1111-2222-3333-444455556666" "users.svc" "10.2.3.4:8080"\n'
    obs_, rep = ingest_bytes("envoy.log", envoy.encode())
    sp = span_of(obs_[0])
    assert rep[0]["format"] == "access" and sp["service"] == "users.svc" and sp["duration_ms"] == 37 and sp["error"] == 1
    assert sp["trace_id"] == "9a1b2c3d-1111-2222-3333-444455556666" and sp["start"] == obs_[0].timestamp      # Envoy stamps the start
    iis = ("#Fields: date time s-ip cs-method cs-uri-stem cs-uri-query s-port cs-username c-ip cs(User-Agent) sc-status sc-substatus sc-win32-status time-taken\n"
           "2026-10-14 10:00:00 10.0.0.1 GET /hr/api/employee/48213/payslips month=3 443 TURKCELL\\ayse 10.1.1.1 Mozilla 200 0 0 245\n")
    sp = span_of(ingest_bytes("u_ex.log", iis.encode())[0][0])
    assert sp["duration_ms"] == 245 and sp["operation"] == "GET /hr/api/employee/{id}/payslips" and sp["user_ref"].startswith("u_") and "ayse" not in json.dumps(sp, default=str)
    apache = '10.0.0.5 - bob [14/Oct/2026:10:00:00 +0000] "GET /x HTTP/1.1" 200 512 "-" "curl/8" 182345 9a1b2c3d11112222333344445555666f\n'
    sp = span_of(ingest_bytes("access.log", apache.encode())[0][0])
    assert round(sp["duration_ms"]) == 182 and sp["trace_id"] == "9a1b2c3d11112222333344445555666f"


def test_ids_and_durations_from_application_logs():
    sp = span_of(obs({"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "elapsed": "1.25s"}, "charge done"))
    assert sp["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736" and sp["parent_id"] == "00f067aa0ba902b7" and sp["duration_ms"] == 1250
    sp = span_of(obs({}, "GET /api/v1/cart/77 200 45ms request_id=r-778899"))
    assert sp["trace_id"] == "r-778899" and sp["duration_ms"] == 45 and sp["operation"] == "GET /api/v1/cart/{id}"
    sp = span_of(obs({"correlation_id": "c1"}, "Payment declined", sev="ERROR"))
    assert sp["kind"] == "log" and sp["error"] == 1 and sp["duration_ms"] == 0
    sp = span_of(obs({"session_id": "s9", "duration_ms": 30}, "page"))
    assert sp["trace_id"] is None and sp["link"] == "session" and sp["confidence"] == 0.7
    assert span_of(obs({}, "plain log line without timing")) is None
    sp = span_of(obs({"trace_id": "t", "duration_ms": 12, "db.system": "oracle"}, "SELECT * FROM emp WHERE id = 5"))
    assert sp["kind"] == "db"


def test_histogram_math():
    h = [0] * (len(BOUNDS) + 1)
    for v in [10] * 90 + [400] * 9 + [5000]:
        h[bucket(v)] += 1
    assert quantile(h, 0.5) <= 10 and 300 <= quantile(h, 0.95) <= 500 and quantile(h, 0.995) >= 3000
    assert apdex(h, 500) == pytest.approx(0.99 + 0.0, abs=0.02)
    assert apdex([0] * len(h), 500) is None and quantile([0] * len(h), 0.5) is None


def _chain(n=120, err_every=40, slow_every=25):
    now = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    rng = random.Random(7); lines = []
    for i in range(n):
        t = now + timedelta(seconds=i); tid = "%032x" % rng.getrandbits(128); root = "%016x" % rng.getrandbits(64)
        err = i % err_every == 0; d_db = 400 if i % slow_every == 0 else rng.randint(5, 30)
        lines += [{"ts": t.isoformat(), "level": "error" if err else "info", "service": "api-gateway", "msg": f"POST /api/orders/{1000 + i} {500 if err else 201}",
                   "trace_id": tid, "span_id": root, "duration_ms": d_db + 60, "http.status_code": 500 if err else 201, "http.method": "POST"},
                  {"ts": (t + timedelta(milliseconds=2)).isoformat(), "level": "info", "service": "order-api", "msg": "create order", "trace_id": tid,
                   "span_id": "a" + root[1:], "parent_span_id": root, "duration_ms": d_db + 50, "user.id": "ayse@turkcell.com.tr"},
                  {"ts": (t + timedelta(milliseconds=5)).isoformat(), "level": "info", "service": "payment-api", "msg": "charge card 4111 1111 1111 1111",
                   "trace_id": tid, "duration_ms": d_db + 20},
                  {"ts": (t + timedelta(milliseconds=8)).isoformat(), "level": "info", "service": "payment-api", "msg": "SELECT * FROM payments WHERE id=?",
                   "trace_id": tid, "duration_ms": d_db, "db.system": "postgresql"}]
    return ingest_bytes("app.jsonl", ("\n".join(json.dumps(x) for x in lines) + "\n").encode())[0]


def test_store_services_detail_search_and_waterfall():
    st = store_for_observations(_chain())
    svcs = {s["service"]: s for s in st.services(minutes=None)}
    assert set(svcs) == {"api-gateway", "order-api", "payment-api"}
    gw = svcs["api-gateway"]
    assert gw["calls"] == 120 and gw["errors"] == 3 and gw["error_rate"] == 0.025 and gw["p50_ms"] < gw["p99_ms"] and 0 < gw["apdex"] <= 1 and gw["series"]
    d = st.service("payment-api", minutes=None)
    assert d["db_calls"][0]["calls"] == 120 and d["operations"][0]["operation"] == "charge card [KART]" and d["slow_traces"]
    hits = st.search(minutes=None, order="duration", limit=5)
    assert hits[0]["duration_ms"] >= 400 and hits[0]["root_service"] == "api-gateway" and hits[0]["links"] == ["explicit"]
    assert hits[0]["user_ref"].startswith("u_")
    w = st.trace(hits[0]["trace_id"])
    by = {(s["service"], s["kind"]): s for s in w["spans"]}
    assert by[("payment-api", "db")]["depth"] == 3 and by[("payment-api", "db")]["parent_inferred"] and not by[("order-api", "internal")]["parent_inferred"]
    assert w["services"][0][0] == "payment-api" and w["inferred_parents"] == 2
    assert st.search(minutes=None, errors=True, limit=50) and all(h["error"] for h in st.search(minutes=None, errors=True))
    assert "4111" not in json.dumps(w) and "ayse" not in json.dumps(w)


def test_sampling_keeps_whole_traces_and_every_failure(tmp_path):
    from watchover.db import Database
    st = TraceStore(Database(str(tmp_path / "t.db")), settings=lambda: {"trace_sample_pct": 20, "trace_slow_ms": 1000})
    st.add_events(_chain(n=300, slow_every=10 ** 9))
    st.flush()
    rows = st.db._exec("SELECT trace_id, COUNT(*) AS n, MAX(error) AS e FROM spans GROUP BY trace_id")
    full = [r for r in rows if r["n"] == 4]
    assert 30 <= len(full) <= 100                                           # about 20 % of 300 traces, each kept whole
    assert sum(1 for r in rows if r["e"]) >= 300 // 40                     # every failed span kept even outside the sample
    assert sum(s["calls"] for s in st.services(minutes=None) if s["service"] == "api-gateway") == 300       # rollups count 100 %
    assert st.prune()["spans_removed"] == 0


def test_waterfall_infers_parents_and_self_time():
    base = dict(trace_id="t", status=0, error=0, env="", host="", link="explicit", confidence=1, ref="r", message="", severity="INFO", attrs="{}", user_ref="", session_id="")
    rows = [dict(base, span_id="r", parent_id=None, start_ms=0, duration_ms=462, service="gw", operation="POST /o", kind="server"),
            dict(base, span_id="a", parent_id="r", start_ms=2, duration_ms=457, service="order", operation="create", kind="internal"),
            dict(base, span_id=None, parent_id=None, start_ms=5, duration_ms=434, service="pay", operation="charge", kind="internal"),
            dict(base, span_id=None, parent_id=None, start_ms=8, duration_ms=400, service="pay", operation="SELECT", kind="db")]
    w = waterfall(rows)
    assert [s["depth"] for s in w["spans"]] == [0, 1, 2, 3] and [s["self_ms"] for s in w["spans"]] == [5, 23, 34, 400]
    assert w["services"][0] == ("pay", 434.0)


def test_privacy_layer(monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k1")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)
    t = scrub("ayse.yilmaz@turkcell.com.tr 10000000146 0532 123 45 67 TR33 0006 1005 1978 6457 8413 26 4111 1111 1111 1111 password=abc Bearer eyJhbGciOiJIUzI1NiJ9.abc.def order 123456")
    for bad in ("ayse", "10000000146", "0532", "TR33", "4111", "abc", "eyJ"):
        assert bad not in t, (bad, t)
    assert "order 123456" in t and scrub("12345678901") == "12345678901"           # not a valid TCKN: left alone
    a = pseudonym("AYSE"); assert a == pseudonym("ayse") and a.startswith("u_") and pseudonym("-") == ""
    monkeypatch.setattr(pv, "_KEY", b"other-install")
    assert pseudonym("ayse") != a                                                   # another installation cannot link people
    assert path_template("/hr/api/employee/48213/payslips?month=3") == "/hr/api/employee/{id}/payslips"
    assert path_template("https://x/api/u/9a1b2c3d-1111-2222-3333-444455556666/o") == "/api/u/{id}/o"


def test_apm_api_live_and_dataset(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    app = create_app(); svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Watchover!Admin2026"}).json()["token"]}
        from watchover.live import simulate_batch
        rng = random.Random(3)
        for _ in range(6):
            svc.live.ingest("sim.jsonl", simulate_batch(rng, 9, incident=True, traces=True), "simulator")
        r = c.get("/api/apm/services", headers=h).json()
        names = {s["service"] for s in r["services"]}
        assert {"api-gateway", "checkout-api"} <= names or {"api-gateway", "search-api"} <= names or "api-gateway" in names
        gw = c.get("/api/apm/services/api-gateway", headers=h).json()
        assert gw["summary"]["calls"] > 0 and gw["series"]
        tr = c.get("/api/traces", params={"order": "duration", "limit": 5}, headers=h).json()
        assert tr and tr[0]["spans"] >= 2
        w = c.get(f"/api/traces/{tr[0]['trace_id']}", headers=h).json()
        assert w["spans"][0]["depth"] == 0 and any(s["kind"] == "db" for s in w["spans"])
        assert c.get("/api/traces/nope", headers=h).status_code == 404
        # an uploaded dataset gets the same views
        up = c.post("/api/datasets", files={"files": ("app.jsonl", ("\n".join(json.dumps({"ts": o.timestamp.isoformat(), "level": o.severity.lower(), "service": o.service, "msg": o.message, **o.attributes}) for o in _chain(40)) + "\n").encode())}, headers=h)
        assert up.status_code in (200, 201, 202), up.text
        import time
        key = None
        for _ in range(60):
            ds = c.get("/api/datasets", headers=h).json()
            if ds:
                key = ds[-1]["id"]; break
            time.sleep(0.5)
        assert key
        r = c.get("/api/apm/services", params={"dataset": key}, headers=h).json()
        assert {s["service"] for s in r["services"]} >= {"api-gateway", "order-api"}
        viewer = c.get("/api/apm/services", headers={})
        assert viewer.status_code == 401
