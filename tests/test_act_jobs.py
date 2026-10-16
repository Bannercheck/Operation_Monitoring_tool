"""WP11a: the pull job channel, server side (docs/design/action-agent.md §9.3.2–§9.3.6; C1, R4, R6, R9, I13 channel part)."""
import base64
import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from watchover.act import keyd as keydmod
from watchover.act.fake_actuator import FakeActuator, direct, http
from watchover.act.jobs import HostLocked, ed25519_verify
from watchover.act.keyd import KeyStore
from watchover.act.types import Trigger, canonical

ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")


@pytest.fixture
def keyd(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="wj"))
    ks = KeyStore(d / "keys", durable=False)
    ks.create()
    srv = keydmod.start_thread(ks, unix=str(d / "keyd.sock"))
    monkeypatch.setenv("WATCHOVER_KEYD", f"unix:{d / 'keyd.sock'}")
    monkeypatch.setenv("WATCHOVER_KEYD_TIMEOUT_MS", "2000")
    yield ks
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def c(tmp_path, monkeypatch, keyd):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app()
    svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        svc.act.set_mode("guided", by="admin@watchover.local")
        yield cl


def _admin(c):
    r = c.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    return {"Authorization": "Bearer " + r.json()["token"]}


def actuator(c, host="web-prod-07", confirm=True):
    """An enrolled collector, its actuator token (POST /api/act/actuators), a fake actuator with a confirmed key."""
    rec, wo = c.svc.agents.enroll(host)
    h = _admin(c)
    r = c.post("/api/act/actuators", json={"agent_id": rec["id"], "criticality": "normal"}, headers=h)
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    assert tok.startswith("wa_") and "--dispatch-pub" in r.json()["install"]
    fa = FakeActuator(tok, host, direct(c.svc.act.jobs), dispatch_pub=c.svc.act.keys.public()["dispatch"], agent_id=rec["id"])
    code, _h, body = fa.hello()
    assert code == 200 and body["key_status"] == "unconfirmed" and body["fingerprint"] == fa.fingerprint
    if confirm:
        r = c.post(f"/api/act/actuators/{rec['id']}/confirm-key", json={"fingerprint": fa.fingerprint}, headers=h)
        assert r.status_code == 200 and r.json()["key_status"] == "confirmed", r.text
        fa.signed = True
    return fa, rec, wo


def diag(c, host, case_id=None, run_id=None, mode="diagnose", step_id=None, template="linux.disk.usage"):
    return c.svc.act.jobs.dispatch(host=host, template_id=template, template_sha256="", params={"path": "/var"}, mode=mode,
                                   case_id=case_id, run_id=run_id, step_id=step_id)


# ---------------------------------------------------------------- refusals on the Python receiver (C1)
def test_python_receiver_refusals(c, monkeypatch):
    from watchover.live import start_receiver
    fa, rec, wo = actuator(c)
    srv = start_receiver(c.svc.live, 0, "shared-key", c.svc.agents, None, jobs=c.svc.act.jobs)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def req(method, path, tok, body=b""):
            r = urllib.request.Request(base + path, data=body if method == "POST" else None, method=method,
                                       headers={"Authorization": f"Bearer {tok}"} if tok else {})
            try:
                with urllib.request.urlopen(r, timeout=5) as x:
                    return x.status, x.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        for tok in ("", wo, "shared-key", "wa_unknown"):                  # collector token, shared key, nothing: 401 on /agent/v1/
            code, body = req("GET", "/agent/v1/jobs", tok)
            assert code == 401 and json.loads(body)["error"] == "actuator token required", tok
        assert req("POST", "/ingest", fa.token, b"line\n")[0] == 401        # wa_ refused on ingest and enrol
        assert req("POST", "/enroll", fa.token, b"{}")[0] == 401
        assert req("POST", "/ingest", wo, b"line\n")[0] == 200
        fh = FakeActuator(fa.token, fa.host, http(base), key=fa.key, agent_id=rec["id"])
        fh.signed = True
        assert fh.request("GET", "/agent/v1/jobs?after=0")[0] == 204          # the real route, signed
        assert req("POST", "/agent/v1/hello", fa.token, b"x" * ((1 << 20) + 1))[0] == 413
    finally:
        srv.shutdown()
        srv.server_close()


def test_open_mode_never_reaches_the_job_channel(c):
    from watchover.live import start_receiver
    srv = start_receiver(c.svc.live, 0, None, None, None, jobs=c.svc.act.jobs)    # no key, no registry: open mode for /ingest
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = urllib.request.Request(base + "/agent/v1/hello", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r, timeout=5)
        assert e.value.code == 401
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- the channel
def test_poll_204_from_memory_and_only_hello_before_confirmation(c, monkeypatch):
    fa, rec, _ = actuator(c, confirm=False)
    assert fa.request("GET", "/agent/v1/jobs?after=0")[0] == 403          # only hello until a person confirms the key
    diag(c, fa.host)
    assert fa.request("GET", "/agent/v1/jobs?after=0")[0] == 403          # no job is ever issued before confirmation
    assert c.post(f"/api/act/actuators/{rec['id']}/confirm-key", json={"fingerprint": "SHA256:wrong"}, headers=_admin(c)).status_code == 409
    c.post(f"/api/act/actuators/{rec['id']}/confirm-key", json={"fingerprint": fa.fingerprint}, headers=_admin(c))
    fa.signed = True
    assert fa.run_once()                                                    # the queued job is delivered now
    calls = []
    real = c.svc.kb._exec
    monkeypatch.setattr(c.svc.kb, "_exec", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    code, hdrs, body = fa.request("GET", "/agent/v1/jobs?after=1")
    assert code == 204 and hdrs["X-Poll-After"] == "10" and calls == []    # 204 from memory: no database hit


def test_unsigned_or_stale_requests_refused(c):
    fa, rec, _ = actuator(c)
    assert fa.request("GET", "/agent/v1/jobs?after=0", sign=False)[0] == 401
    old = str(int((datetime.now(timezone.utc) - timedelta(seconds=300)).timestamp()))
    code, _h, body = fa.request("GET", "/agent/v1/jobs?after=0", ts=old)
    assert code == 401 and "skew" in body["error"]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    impostor = FakeActuator(fa.token, fa.host, fa.t, key=Ed25519PrivateKey.generate())
    impostor.signed = True
    assert impostor.request("GET", "/agent/v1/jobs?after=0")[0] == 401     # a stolen token alone is not the identity (R4)
    assert c.svc.act.kill.killed(host=fa.host)                              # bad_sig: host stop and token rotation
    assert fa.request("GET", "/agent/v1/jobs?after=0")[0] == 401             # the old token no longer works


def test_lease_seq_strictly_increasing_signed_and_one_result(c, keyd):
    fa, rec, _ = actuator(c)
    jobs = [diag(c, fa.host) for _ in range(3)]
    got = fa.run_once(results={"linux.disk.usage": {"mount": "/var", "use_pct": 91}})
    assert [g["reject"] for g in got] == ["", ""] and len(got) == 2          # ≤ 2 diagnose jobs per poll
    got += fa.run_once(results={"linux.disk.usage": {"mount": "/var", "use_pct": 91}})
    seqs = [g["seq"] for g in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3 and seqs[0] >= 1
    j = c.svc.act.jobs.get(jobs[0]["job_id"])
    env = json.loads(j["envelope"])
    assert ed25519_verify(c.svc.act.keys.public()["dispatch"], env["sig"], b"wo-job-v1\n" + canonical(env["body"]).encode())
    assert j["state"] == "done" and j["result_sig_ok"] == 1 and json.loads(j["result"])["fields"]["use_pct"] == 91
    # one result per job_id: a re-sent (or forged second) result changes nothing
    rb = {"v": 1, "job_id": j["job_id"], "nonce": j["nonce"], "seq": j["seq"], "status": "failed", "fields": {"use_pct": 1}}
    code, _h, body = fa.request("POST", f"/agent/v1/jobs/{j['job_id']}/result", {"body": rb, "sig": fa._sig("result", rb)})
    assert body == {"ok": True, "duplicate": True} and c.svc.act.jobs.get(j["job_id"])["state"] == "done"
    # keyd refuses a seq that is not above the last one it signed for this agent (R6)
    with pytest.raises(keydmod.KeydError):
        keyd.sign_job({**env["body"], "job_id": "J-x"})
    ev = [e["event"] for e in c.svc.act.audit.events(object_kind="job", limit=50)]
    assert ev.count("job.issued") == 3 and ev.count("job.result") == 3 and ev.count("job.queued") == 3


def test_lease_then_silence_is_unknown(c):
    fa, rec, _ = actuator(c)
    j = diag(c, fa.host, mode="execute", template="linux.logs.compress_old")
    code, _h, body = fa.request("GET", "/agent/v1/jobs?after=0")
    assert code == 200 and body["jobs"][0]["body"]["job_id"] == j["job_id"]   # leased, then the actuator goes silent
    late = datetime.now(timezone.utc) + timedelta(seconds=300 + 120 + 61)
    assert c.svc.act.jobs.sweep(late)["unknown"] == 1
    assert c.svc.act.jobs.get(j["job_id"])["state"] == "unknown"
    assert c.svc.kb._exec("SELECT host FROM act_host_locks")                   # unknown keeps the host lock: verify decides


def test_two_concurrent_dispatches_one_lock(c):
    fa, rec, _ = actuator(c)
    res, errs = [], []

    def go(i):
        try:
            res.append(c.svc.act.jobs.dispatch(host=fa.host, template_id="linux.logs.compress_old", template_sha256="", params={}, mode="execute",
                                               run_id=100 + i, step_id=200 + i))
        except HostLocked as e:
            errs.append(e)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(res) == 1 and len(errs) == 1 and errs[0].detail()["code"] == "act_host_locked"
    assert len(c.svc.kb._exec("SELECT * FROM act_host_locks")) == 1
    diag(c, fa.host)                                                           # read-only jobs take no lock


def test_ledger_mismatch_stops_the_host_and_rotates_the_token(c):
    fa, rec, _ = actuator(c)
    code, _h, body = fa.hello(extra={"ledger_lines": [{"n": 1, "envelope_sha256": "f" * 64, "job_id": "J-forged"}]})
    assert code == 200 and body["ledger_mismatch"]
    assert c.svc.act.kill.killed(host=fa.host)["reason"].startswith("Ajan defteri ile sunucu kaydı uyuşmuyor")
    assert fa.request("GET", "/agent/v1/jobs?after=0")[0] == 401              # the token was rotated
    ev = [e["event"] for e in c.svc.act.audit.events(object_kind="actuator", limit=20)]
    assert "ledger.mismatch" in ev and "actuator.token_rotated" in ev


def test_fail_closed_keyd_down_skew_and_kill(c, monkeypatch):
    fa, rec, _ = actuator(c)
    j = diag(c, fa.host)
    monkeypatch.setenv("WATCHOVER_KEYD", "unix:/nonexistent/keyd.sock")
    c.svc.act.keys._st = (0.0, None)
    code, _h, body = fa.request("GET", "/agent/v1/jobs?after=0")
    assert code == 200 and body["jobs"] == [] and c.svc.act.jobs.get(j["job_id"])["state"] == "queued"   # nothing leased without keyd
    monkeypatch.undo()
    c.svc.act.keys._st = (0.0, None)
    fa.hello(clock="2020-01-01T00:00:00Z")                                     # clock skew > 120 s: no job (I13)
    code, _h, _b = fa.request("GET", "/agent/v1/jobs?after=0")
    assert code == 204
    fa.hello()
    c.svc.act.kill.stop("host", fa.host, by="ops@x", reason="bakım")
    code, _h, body = fa.request("GET", "/agent/v1/jobs?after=0")
    assert code == 204 or (body["jobs"] == [] and body["halt"])              # kill checked at agent fetch (I7)


def test_round_trip_from_the_case_screen(c):
    """The demo check: a case on web-prod-07, its diagnose job through the fake actuator, a signed diag result as evidence."""
    fa, rec, _ = actuator(c)
    cid = c.svc.act.cases.route(Trigger(source="external", key="external:rt:1", host=fa.host, title="disk"))["case_id"]
    diag(c, fa.host, case_id=cid, run_id=5)
    fa.run_once(results={"linux.disk.usage": {"mount": "/var", "use_pct": 93}})
    d = c.get(f"/api/act/cases/{cid}", headers=_admin(c)).json()
    e = [x for x in d["evidence"] if x["kind"] == "diag"][0]
    assert e["source"] == "trusted_signed" and d["slots"]["mount"][0]["value"] == "/var"
    assert c.svc.act.evidence.cap(cid, run_id=5)["level"] == 4
    lst = c.get("/api/act/actuators", headers=_admin(c)).json()
    assert lst[0]["host"] == fa.host and lst[0]["key_status"] == "confirmed" and "token_sha256" not in lst[0]


def test_auth_json_publishes_actuators(c, tmp_path):
    from watchover.spool_consumer import SpoolConsumer
    fa, rec, _ = actuator(c)
    sc = SpoolConsumer(str(tmp_path / "spool"), c.svc.live, c.svc.agents, key_fn=lambda: "")
    st = sc.auth_state()
    import hashlib
    assert st["actuators"][hashlib.sha256(fa.token.encode()).hexdigest()] == {"agent_id": rec["id"], "host": fa.host}
    assert all(v["name"] != fa.token for v in st["agents"].values())
