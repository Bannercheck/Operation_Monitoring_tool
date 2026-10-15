"""User experience (web and mobile): KVKK masking on intake, sessions and steps in the trace model, the link from a user step
to the backend trace by traceparent, app keys / origins / sampling, the API with its audited user lookup, and the browser
sensor end to end in a real Chromium (skipped when Node or Chromium is missing)."""
import json
import random
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from watchover.db import Database
from watchover.live import LiveStore, rum_request, simulate_rum, start_receiver
from watchover.rum import RumStore
from watchover.traces import TraceStore

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "test-key")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)
    db = Database(str(tmp_path / "k.db"))
    tr = TraceStore(db, settings=lambda: {"trace_sample_pct": 100})
    live = LiveStore(spool=str(tmp_path / "live.jsonl"))
    live.on_ingest = tr.add_events
    return RumStore(db, tr, live), tr, live


def mobile_doc(now_ms, trace="ab" * 16):
    return {"session": "sess-1", "platform": "android", "version": "3.4.1", "user": "P48213", "env": "prod",
            "device": {"os": "Android 14", "model": "SM-S911B", "net": "cellular", "carrier": "Turkcell"},
            "events": [{"t": "start", "ts": now_ms, "dur": 1800, "cold": True},
                       {"t": "view", "ts": now_ms + 2000, "name": "Bordro", "dur": 900, "trace": trace, "span": "11" * 8},
                       {"t": "http", "ts": now_ms + 2010, "method": "GET", "url": "https://ik.example/api/employee/48213/payslips?month=3&tckn=10000000146",
                        "status": 503, "dur": 850, "trace": trace, "span": "22" * 8, "parent": "11" * 8},
                       {"t": "error", "ts": now_ms + 2900, "msg": "Bordro yüklenemedi: personel 10000000146 ayse@turkcell.com.tr", "trace": trace},
                       {"t": "crash", "ts": now_ms + 3000, "msg": "IllegalStateException", "stack": "at Payslip.bind(Payslip.kt:88) user 0532 123 45 67"},
                       {"t": "action", "ts": now_ms + 4000, "name": "click: Bordro indir 4111 1111 1111 1111", "dur": 300}]}


def test_mobile_intake_masks_and_links(env):
    rum, tr, live = env
    app = rum.create_app("ik-mobil", "android")
    now_ms = datetime.now(UTC).timestamp() * 1000
    res = rum.ingest(app, mobile_doc(now_ms))
    assert res["session"] == "sess-1" and res["spans"] == 6 and res["errors"] == 2
    blob = json.dumps(tr.db._exec("SELECT * FROM spans")) + json.dumps(rum.sessions(minutes=None))
    for bad in ("48213", "10000000146", "ayse", "0532", "4111", "P48213", "month=3"):
        assert bad not in blob, bad
    s = rum.sessions(minutes=None)[0]
    assert s["platform"] == "android" and s["views"] == 2 and s["actions"] == 1 and s["requests"] == 1 and s["errors"] == 1 and s["crashes"] == 1
    assert s["user_ref"].startswith("u_") and s["os"] == "Android 14" and s["carrier"] == "Turkcell" and s["entry"] == "Bordro"
    # backend log carrying the same traceparent joins the user's step by id
    live.ingest("hr.jsonl", (json.dumps({"ts": datetime.fromtimestamp((now_ms + 2030) / 1000, UTC).isoformat(), "level": "error", "service": "hr-api",
                                          "msg": "GET /api/employee/48213/payslips 503", "traceparent": f"00-{'ab' * 16}-{'22' * 8}-01", "duration_ms": 700,
                                          "http.status_code": 503}) + "\n").encode(), "hr-app-01")
    w = tr.trace("ab" * 16)
    kinds = [s["kind"] for s in w["spans"]]
    assert "mobile" in kinds and "client" in kinds and "server" in kinds
    srv = next(x for x in w["spans"] if x["service"] == "hr-api" and x["kind"] == "server")
    assert srv["parent_key"] == "2222222222222222" and not srv["parent_inferred"]          # explicit parent: the phone's http span
    sess = rum.session("sess-1")
    assert [x["step"] for x in sess["steps"]][:3] == ["start", "view", "http"] and sess["steps"][1]["backend"]["services"] == 1
    # errors and crashes reached the live feed as ERROR / CRITICAL events for the anomaly engine
    sev = [o.severity for o in live.snapshot() if o.service == "ik-mobil"]
    assert sorted(sev) == ["CRITICAL", "ERROR"]
    ov = rum.overview(minutes=None)
    a = ov["apps"][0]
    assert a["sessions"] == 1 and a["crash_free"] == 0.0 and ov["app_start"]["ik-mobil"]["p50"] == 1800 and ov["errors"]


def test_web_vitals_grades_and_apps(env):
    rum, tr, _ = env
    app = rum.create_app("ik-portal", "web", origins="https://ik.example.com, *.turkcell.com.tr")
    now_ms = datetime.now(UTC).timestamp() * 1000
    for i, lcp in enumerate([1200, 1800, 2300, 5200]):
        rum.ingest(app, {"session": f"w{i}", "platform": "web", "events": [{"t": "view", "ts": now_ms, "name": "/payslips/77881?x=1", "dur": 900,
                                                                            "vitals": {"lcp": lcp, "cls": 0.02, "inp": 120, "ttfb": 300}}]},
                   ua="Mozilla/5.0 (Windows NT 10.0; Win64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36")
    ov = rum.overview(minutes=None)
    v = ov["vitals"]["ik-portal"]
    assert v["lcp"]["p75"] == 5200 and v["lcp"]["grade"] == "poor" and v["cls"]["grade"] == "good" and v["inp"]["grade"] == "good"
    assert rum.sessions(minutes=None)[0]["browser"] == "Chrome" and rum.sessions(minutes=None)[0]["os"] == "Windows"
    assert any(s["name"] == "view /payslips/{id}" for s in ov["slow_steps"])
    assert rum.origin_allowed(app, "https://ik.example.com") and rum.origin_allowed(app, "https://hr.turkcell.com.tr") and not rum.origin_allowed(app, "https://evil.example")
    assert rum.origin_allowed(app, None)                                                   # mobile SDKs send no Origin
    with pytest.raises(ValueError):
        rum.create_app("ik-portal")
    k1 = app["key"]; app2 = rum.rotate_key(app["id"])
    assert app2["key"] != k1 and rum.app_by_key(k1) is None
    rum.update_app(app["id"], status="disabled")
    assert rum.app_by_key(app2["key"]) is None


def test_rum_request_checks(env):
    rum, _, _ = env
    app = rum.create_app("web1", "web", origins="https://a.example", sample_pct=100)
    assert rum_request(rum, "nope", None, "", b"{}")[0] == 401
    assert rum_request(rum, app["key"], "https://b.example", "", b"{}")[0] == 403
    assert rum_request(rum, app["key"], "https://a.example", "", b"not json")[0] == 400
    code, body = rum_request(rum, app["key"], "https://a.example", "", json.dumps({"session": "s", "events": []}).encode())
    assert code == 202 and body["ok"]
    rum.update_app(app["id"], sample_pct=1)
    kept = sum(rum_request(rum, app["key"], None, "", json.dumps({"session": f"s{i}", "events": []}).encode())[1].get("sampled", True) for i in range(300))
    assert kept < 30                                                                       # ~1 % of sessions


def test_simulator_sessions(env):
    rum, tr, live = env
    rng = random.Random(4)
    for app in ("ik-mobil", "ik-portal"):
        rum.create_app(app, "android" if app == "ik-mobil" else "web")
    apps = {a["name"]: a for a in rum.apps()}
    for _ in range(8):
        sessions, backend = simulate_rum(rng, incident=True)
        live.ingest("hr.jsonl", ("\n".join(backend) + "\n").encode(), "sim")
        for a, doc in sessions:
            rum.ingest(apps[a], doc)
    ov = rum.overview(minutes=None)
    assert {a["app"] for a in ov["apps"]} == {"ik-mobil", "ik-portal"} and ov["vitals"].get("ik-portal")
    s = rum.sessions(minutes=None)[0]
    det = rum.session(s["session_id"])
    assert any(x["backend"] for x in det["steps"] if x["step"] == "http")                 # a phone call joined hr-api by traceparent


def test_rum_api_and_audited_lookup(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    appx = create_app(); svc = appx.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(appx) as c:
        h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Watchover!Admin2026"}).json()["token"]}
        a = c.post("/api/rum/apps", json={"name": "ik-mobil", "platform": "android"}, headers=h)
        assert a.status_code == 201 and a.json()["key"].startswith("pk_")
        assert c.get("/api/rum/apps", headers=h).json()["receiver"].endswith(":8600")
        now_ms = datetime.now(UTC).timestamp() * 1000
        svc.rum.ingest(a.json(), mobile_doc(now_ms))
        ov = c.get("/api/rum/overview", headers=h).json()
        assert ov["apps"][0]["app"] == "ik-mobil"
        ss = c.get("/api/rum/sessions", params={"errors": "true"}, headers=h).json()
        assert ss and ss[0]["session_id"] == "sess-1"
        assert c.get("/api/rum/sessions/sess-1", headers=h).json()["steps"]
        ts = c.get(f"/api/traces/{'ab' * 16}/session", headers=h).json()          # trace detail -> its client session
        assert ts["kind"] == "rum" and ts["session"] == "sess-1" and ts["model"] == "SM-S911B" and ts["version"] == "3.4.1"
        assert ts["trigger"]["name"] == "view Bordro" and ts["trigger"]["this"] and 1 <= ts["position"] <= ts["steps_total"]
        assert "P48213" not in str(ts) and "10000000146" not in str(ts)
        assert c.get("/api/traces/nope/session", headers=h).status_code == 404
        assert c.post("/api/rum/lookup", json={"user": "P48213", "reason": ""}, headers=h).status_code == 400
        lk = c.post("/api/rum/lookup", json={"user": "P48213", "reason": "destek talebi 1234"}, headers=h).json()
        assert lk["user_ref"] == ss[0]["user_ref"] and lk["sessions"] == 1
        ev = c.get("/api/users/events", headers=h).json()
        assert any(e["event"] == "rum.user_lookup" and "destek" in e["detail"] for e in ev)
        assert c.get("/api/rum/overview").status_code == 401


# ---------------------------------------------------------------- the browser sensor in a real Chromium
NODE = shutil.which("node")
CHROME = Path("/opt/pw-browsers/chromium")
PW = next((p for p in (Path("/opt/node22/lib/node_modules/playwright"), Path("/usr/lib/node_modules/playwright")) if p.exists()), None)


def _free() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.mark.skipif(not (NODE and CHROME.exists() and PW), reason="node + playwright + chromium needed")
def test_browser_sensor_end_to_end(env, tmp_path):
    rum, tr, live = env
    seen = []
    page_port, recv_port = _free(), _free()
    app = rum.create_app("ik-portal", "web", origins=f"http://127.0.0.1:{page_port}")
    html = f"""<!doctype html><html><head><title>IK</title><script src="http://127.0.0.1:{recv_port}/rum.js" data-key="{app['key']}" data-env="test"></script></head>
<body><h1>Bordro</h1><input id="tckn" value="10000000146"><button id="b" onclick="fetch('/api/employee/48213/payslips?month=3').then(r=>r.text()).then(t=>document.title=t)">Bordro indir</button>
<button id="e" onclick="setTimeout(function(){{throw new Error('boom 10000000146')}},0)">Hata</button></body></html>"""

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api/"):
                seen.append(self.headers.get("traceparent"))
                time.sleep(0.15)
                body = b"ok"
            else:
                body = html.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html" if not self.path.startswith("/api/") else "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    page = ThreadingHTTPServer(("127.0.0.1", page_port), Page); threading.Thread(target=page.serve_forever, daemon=True).start()
    recv = start_receiver(live, recv_port, None, None, rum)
    script = tmp_path / "run.mjs"
    script.write_text(f"""import {{ chromium }} from "{PW}/index.mjs";
const b = await chromium.launch({{ executablePath: "{CHROME}" }});
const p = await b.newPage();
await p.goto("http://127.0.0.1:{page_port}/hr", {{ waitUntil: "load" }});
await p.waitForTimeout(400);
await p.click("#b"); await p.waitForFunction(() => document.title === "ok");
await p.click("#e"); await p.waitForTimeout(300);
await p.evaluate(() => window.watchover.setUser("P48213"));
await p.evaluate(() => window.watchover.flush()); await p.waitForTimeout(1500);
await p.evaluate(() => {{ history.pushState({{}}, "", "/hr/leave/5521"); }}); await p.waitForTimeout(1300);
await p.evaluate(() => window.watchover.flush()); await p.waitForTimeout(800);
await b.close();
""")
    try:
        r = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]
        assert seen and seen[0] and seen[0].startswith("00-")
        trace = seen[0].split("-")[1]
        w = tr.trace(trace)
        assert w is not None
        kinds = {s["kind"] for s in w["spans"]}
        assert {"user", "client"} <= kinds                                                 # the click and its call, one trace
        act = next(s for s in w["spans"] if s["kind"] == "user")
        assert act["operation"] == "click: Bordro indir"
        call = next(s for s in w["spans"] if s["kind"] == "client")
        assert call["operation"] == "GET /api/employee/{id}/payslips" and call["duration_ms"] >= 100
        sess = rum.sessions(minutes=None)
        assert len(sess) == 1 and sess[0]["platform"] == "web" and sess[0]["errors"] >= 1 and sess[0]["user_ref"].startswith("u_")
        steps = rum.session(sess[0]["session_id"])["steps"]
        assert any(s["step"] == "view" and s["name"] == "view /hr/leave/{id}" for s in steps)
        blob = json.dumps(tr.db._exec("SELECT * FROM spans")) + json.dumps(sess)
        assert "10000000146" not in blob and "48213" not in blob and "month=3" not in blob
    finally:
        page.shutdown(); recv.shutdown()


def test_user_journeys_are_tail_sampled(tmp_path, monkeypatch):
    """With 1 % sampling, a backend trace that a phone step later claims is still kept whole; one nobody claims is not."""
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k")
    db = Database(str(tmp_path / "k.db"))
    tr = TraceStore(db, settings=lambda: {"trace_sample_pct": 1})
    live = LiveStore(spool=str(tmp_path / "live.jsonl")); live.on_ingest = tr.add_events
    rum = RumStore(db, tr, live); app = rum.create_app("ik-mobil", "android")
    now = datetime.now(UTC)
    claimed, lonely = "c" * 32, "d" * 32
    tr.add_spans([sp for sp in [__import__("watchover.traces", fromlist=["span_of"]).span_of(o) for o in __import__("watchover.pipeline", fromlist=["ingest_bytes"]).ingest_bytes(
        "hr.jsonl", ("\n".join(json.dumps({"ts": now.isoformat(), "level": "info", "service": "hr-api", "msg": "GET /api/x 200", "trace_id": t, "duration_ms": 40})
                                 for t in (claimed, lonely)) + "\n").encode())[0]] if sp])
    assert tr.trace(claimed) is None or tr.sampled(claimed)                           # not stored yet (1 %)
    rum.ingest(app, {"session": "s", "platform": "android", "events": [{"t": "http", "ts": now.timestamp() * 1000, "method": "GET", "url": "/api/x", "status": 200, "dur": 60, "trace": claimed}]})
    w = tr.trace(claimed)
    assert w is not None and {s["service"] for s in w["spans"]} == {"hr-api", "ik-mobil"}
    assert tr.trace(lonely) is None or tr.sampled(lonely)
    late = {"ts": now.isoformat(), "level": "info", "service": "payroll-db", "msg": "SELECT 1", "trace_id": claimed, "duration_ms": 5}
    live.ingest("db.jsonl", (json.dumps(late) + "\n").encode(), "db")                  # arrives after the claim: kept too
    assert "payroll-db" in {s["service"] for s in tr.trace(claimed)["spans"]}

