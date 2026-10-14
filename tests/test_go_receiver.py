"""The Go receiver end to end: the real binary on a free port, the real agent Sender posting to it, the dashboard's spool
consumer turning segments into live events, enrolment proxied to the Python receiver, revocation reaching the Go side through
auth.json. Skipped when no Go toolchain is installed (CI images without Go still run the Go unit tests in receiver/)."""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
pytestmark = pytest.mark.skipif(GO is None, reason="no Go toolchain")


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    out = tmp_path_factory.mktemp("bin") / "wo-receiver"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "receiver", check=True, capture_output=True, timeout=300)
    return out


def test_agent_to_go_to_live_store(binary, tmp_path):
    from watchover.agents import AgentRegistry
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore, start_receiver
    from watchover.spool_consumer import SpoolConsumer
    kb = Knowledge(str(tmp_path / "k.db"))
    reg = AgentRegistry(kb)
    rec, tok = reg.enroll("erp-app-01", env="prod", site="ist")
    store = LiveStore(spool=str(tmp_path / "live.jsonl"))
    spool = tmp_path / "spool"
    cons = SpoolConsumer(str(spool), store, reg, key_fn=lambda: "")
    cons.export_auth(force=True)
    up_port, port = free_port(), free_port()
    upstream = start_receiver(store, up_port, None, reg)                     # the dashboard's internal receiver (enrolment)
    agent_dir = tmp_path / "agentfiles"; agent_dir.mkdir(); shutil.copy(ROOT / "agent.py", agent_dir / "agent.py")
    env = dict(os.environ, WO_LISTEN=f"127.0.0.1:{port}", WO_SPOOL=str(spool), WO_UPSTREAM=f"http://127.0.0.1:{up_port}", WO_AGENT_DIR=str(agent_dir), WO_SEGMENT_MS="100")
    proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait(lambda: _ok(base + "/health"))
        spec = importlib.util.spec_from_file_location("wo_agent", ROOT / "agent.py"); agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
        sender = agent.Sender(base + "/ingest", tok, "erp-app-01", spool=str(tmp_path / "agent-spool"))
        lines = "".join(json.dumps({"ts": "2026-10-14T10:00:0%dZ" % i, "level": "error" if i == 3 else "info", "service": "erp", "msg": f"GET /api/x/{i} 200 {10 + i}ms", "trace_id": f"t{i}"}) + "\n" for i in range(6))
        r = sender.send(lines.encode(), "erp.jsonl")
        assert r and r["accepted"] == 6 and r["queued"] is True and r["agent"] == "erp-app-01"
        assert wait(lambda: cons.pending())                                   # segment closed by the receiver
        assert cons.run_once() == 1
        evs = store.snapshot()
        assert len(evs) == 6 and all(e.environment == "prod" and e.attributes.get("site") == "ist" for e in evs)
        assert reg.get(rec["id"])["events"] == 6 and not cons.pending()
        assert cons.stats()["records"] == 1 and cons.stats()["backlog_segments"] == 0
        # agent files and enrolment go through the Go front door
        assert urllib.request.urlopen(base + "/agent.py").read().startswith(b'"""Watchover agent')
        key = reg.enroll_key()
        req = urllib.request.Request(base + "/enroll", data=json.dumps({"name": "hr-app-02", "env": "prod"}).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        enr = json.loads(urllib.request.urlopen(req).read())
        assert enr["token"].startswith("wo_") and enr["agent"] == "hr-app-02"
        assert cons.export_auth()                                             # the new agent reaches the Go side ...
        time.sleep(2.3)                                                       # ... at its next auth reload (every 2 s)
        assert agent.Sender(base + "/ingest", enr["token"], "hr-app-02").send(b"hello\n", "a.log")["agent"] == "hr-app-02"
        reg.revoke(rec["id"]); cons.export_auth(); time.sleep(2.3)            # revocation too
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/ingest", data=b"x\n", headers={"Authorization": f"Bearer {tok}"}))
        assert e.value.code == 401
        h = json.loads(urllib.request.urlopen(base + "/health").read())
        assert h["engine"] == "go" and h["batches"] == 2 and h["unauthorized"] >= 1
    finally:
        proc.terminate(); proc.wait(10); upstream.shutdown()


def test_consumer_resumes_and_skips_torn_tail(tmp_path):
    from watchover.live import LiveStore
    from watchover.spool_consumer import SpoolConsumer
    store = LiveStore(spool=str(tmp_path / "live.jsonl"))
    d = tmp_path / "spool"; d.mkdir()
    rec = lambda body, **h: (json.dumps({"v": 1, "path": "/ingest", "file": "a.log", "agent": "a", "len": len(body), **h}) + "\n").encode() + body + b"\n"
    seg = d / "seg-0000000000000000001.wo"
    seg.write_bytes(rec(b"line one\n") + rec(b"line two\nline three\n") + b'{"v":1,"len":50}\npartial')
    cons = SpoolConsumer(str(d), store)
    assert cons.run_once() == 2 and cons.torn == 1 and not seg.exists()
    assert len(store.snapshot()) == 3
    seg2 = d / "seg-0000000000000000002.wo"; first = rec(b"already done\n"); seg2.write_bytes(first + rec(b"fresh\n"))
    (d / "seg-0000000000000000002.pos").write_text(str(len(first)))          # a restart inside the segment resumes there
    assert cons.run_once() == 1 and [o.message for o in store.snapshot()][-1] == "fresh"
    st = json.loads((d / "auth.json").read_text())
    assert st["open"] is True and st["agents"] == {} and st["key_sha256"] == ""


def _ok(url: str) -> bool:
    try:
        return urllib.request.urlopen(url, timeout=1).status == 200
    except Exception:  # noqa: BLE001
        return False


def test_rum_through_go_receiver(binary, tmp_path, monkeypatch):
    """A phone's batch to the Go /mobile endpoint reaches the RUM store through the spool, with the app key and origin rules."""
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k")
    from watchover.db import Database
    from watchover.live import LiveStore, rum_request
    from watchover.rum import RumStore
    from watchover.spool_consumer import SpoolConsumer
    from watchover.traces import TraceStore
    db = Database(str(tmp_path / "k.db")); tr = TraceStore(db); store = LiveStore(spool=str(tmp_path / "l.jsonl"))
    rum = RumStore(db, tr, store); app = rum.create_app("ik-mobil", "android"); web = rum.create_app("ik-portal", "web", origins="https://ik.example")
    h = lambda hd, body: rum_request(rum, hd.get("app_key", ""), hd.get("origin") or None, hd.get("ua", ""), body)   # noqa: E731
    spool = tmp_path / "spool"
    cons = SpoolConsumer(str(spool), store, None, handlers={"/rum": h, "/mobile": h}, apps_fn=rum.apps)
    cons.export_auth(force=True)
    port = free_port()
    proc = subprocess.Popen([str(binary)], env=dict(os.environ, WO_LISTEN=f"127.0.0.1:{port}", WO_SPOOL=str(spool), WO_SEGMENT_MS="100"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait(lambda: _ok(base + "/health"))
        doc = {"session": "m1", "platform": "android", "user": "P1", "events": [{"t": "view", "ts": time.time() * 1000, "name": "Bordro", "dur": 300}]}
        r = urllib.request.urlopen(urllib.request.Request(f"{base}/mobile?k={app['key']}", data=json.dumps(doc).encode(), headers={"Content-Type": "application/json"}))
        assert r.status == 202
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(f"{base}/rum?k={web['key']}", data=b"{}", headers={"Origin": "https://evil.example"}))
        assert e.value.code == 403
        assert wait(lambda: cons.pending())
        cons.run_once()
        s = rum.sessions(minutes=None)
        assert len(s) == 1 and s[0]["app"] == "ik-mobil" and s[0]["views"] == 1 and s[0]["user_ref"].startswith("u_")
    finally:
        proc.terminate(); proc.wait(10)

