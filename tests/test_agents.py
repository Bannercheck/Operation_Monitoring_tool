"""Agent identity: enrol / verify / revoke / rotate, the receiver stamps events with the registered identity, settings persist."""
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from watchover import settings
from watchover.agents import AgentRegistry
from watchover.knowledge import Knowledge
from watchover.live import LiveStore, simulate_batch, start_receiver


def test_registry(tmp_path):
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    rec, tok = reg.enroll("db-01", "PROD", "IST-DC1", "oracle,core")
    assert tok.startswith("wo_") and rec["env"] == "prod" and rec["status"] == "active" and reg.verify(tok)["id"] == rec["id"]
    assert reg.verify("wo_nope") is None and reg.verify("") is None
    reg.revoke(rec["id"])
    assert reg.verify(tok) is None and reg.get(rec["id"])["status"] == "revoked"
    new = reg.rotate(rec["id"])
    assert new != tok and reg.verify(new)["name"] == "db-01" and reg.verify(tok) is None
    import pytest
    with pytest.raises(ValueError):
        reg.enroll("DB-01", "prod")                                   # same name, case-insensitive, while active
    reg.touch(rec["id"], "10.0.0.5", 7)
    assert reg.get(rec["id"])["events"] == 7 and reg.list()[0]["last_ip"] == "10.0.0.5"
    reg.delete(rec["id"]); assert reg.list() == []


def test_receiver_uses_agent_identity(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db")); reg = AgentRegistry(kb)
    rec, tok = reg.enroll("edge-07", "staging", "ANK-DC2")
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, "legacy-key", reg)
    url = f"http://127.0.0.1:{srv.server_address[1]}/ingest"
    try:
        def post(body, headers):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        syslog = b"Sep 16 14:31:02 db-01 postgres[123]: latency increased\n"
        out = post(syslog, {"Authorization": f"Bearer {tok}", "X-Agent": "spoofed-name", "X-File-Name": "db.log"})
        assert out == {"accepted": 1, "agent": "edge-07", "identity": "token"}         # registered name wins over the header
        o = store.snapshot()[-1]
        assert o.attributes["agent"] == "edge-07" and o.environment == "staging" and o.attributes["site"] == "ANK-DC2" and o.attributes["agent_id"] == rec["id"]
        assert reg.get(rec["id"])["events"] == 1 and reg.get(rec["id"])["last_seen"]
        assert post(simulate_batch(__import__("random").Random(1), 3), {"X-API-Key": "legacy-key", "X-Agent": "old-agent"})["identity"] == "key"
        reg.revoke(rec["id"])
        for hdr in ({"Authorization": f"Bearer {tok}"}, {"X-API-Key": "wo_unknown"}, {"X-API-Key": "wrong"}):
            try:
                post(syslog, hdr); assert False, hdr
            except urllib.error.HTTPError as e:
                assert e.code == 401
    finally:
        srv.shutdown()


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WATCHOVER_SKIP_SETUP", raising=False)
    assert settings.load()["setup_done"] is False and not settings.setup_done()
    settings.save({"llm_model": "qwen2.5:7b-instruct", "setup_done": True, "bogus": 1})
    cfg = settings.load()
    assert cfg["llm_model"] == "qwen2.5:7b-instruct" and cfg["setup_done"] and "bogus" not in cfg and settings.setup_done()
    assert oct(settings.path().stat().st_mode)[-3:] == "600"


def test_agent_sender_spool_and_self_test(tmp_path):
    import subprocess, sys, time
    from pathlib import Path
    import importlib.util
    spec = importlib.util.spec_from_file_location("wo_agent", Path(__file__).resolve().parents[1] / "agent.py"); ag = importlib.util.module_from_spec(spec); spec.loader.exec_module(ag)
    kb = Knowledge(str(tmp_path / "k.db")); reg = AgentRegistry(kb)
    rec, tok = reg.enroll("web-01", "prod", "IST")
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, None, reg)
    port = srv.server_address[1]
    try:
        dead = ag.Sender("http://127.0.0.1:1/ingest", tok, "web-01", spool=str(tmp_path / "spool"))
        assert dead.send(b'{"level":"error","msg":"x"}\n', "a.jsonl") is None and dead.spooled == 1 and len(list((tmp_path / "spool").glob("*__*"))) == 1
        live = ag.Sender(f"http://127.0.0.1:{port}/ingest", tok, "web-01", spool=str(tmp_path / "spool"))
        out = live.send(b'{"level":"info","msg":"y"}\n', "b.jsonl")
        assert out["identity"] == "token" and out["agent"] == "web-01" and not list((tmp_path / "spool").glob("*__*"))   # spool drained
        assert store.received == 2
        bad = ag.Sender(f"http://127.0.0.1:{port}/ingest", "wo_revoked_or_unknown", "web-01", spool=str(tmp_path / "spool"))
        assert bad.send(b"x\n", "c.log") is None and not list((tmp_path / "spool").glob("*__*"))                   # 401 is never spooled
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "agent.py"), "--url", f"http://127.0.0.1:{port}/ingest", "--token", tok, "--test"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "identity=token" in r.stdout, r.stdout + r.stderr
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "agent.py"), "--url", f"http://127.0.0.1:{port}/ingest", "--token", "wo_bad", "--test"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 2
        # the receiver serves the collector and its installer
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/agent.py", timeout=5).read()
        assert b"Watchover agent" in body
        inst = urllib.request.urlopen(f"http://127.0.0.1:{port}/agent/install.sh", timeout=5).read()
        assert inst.startswith(b"#!/usr/bin/env bash") and b"--uninstall" in inst
        assert json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read())["agents"] >= 1
    finally:
        srv.shutdown()


def test_agent_installer_end_to_end(tmp_path):
    """Run scripts/agent-install.sh unprivileged against a live receiver: downloads agent.py, writes env, passes the self-test."""
    import shutil, subprocess
    from pathlib import Path
    if not shutil.which("bash"):
        return
    kb = Knowledge(str(tmp_path / "k.db")); reg = AgentRegistry(kb)
    rec, tok = reg.enroll("app-03", "staging", "ANK")
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, None, reg)
    port = srv.server_address[1]
    try:
        r = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "agent-install.sh"), "--url", f"http://127.0.0.1:{port}/ingest", "--token", tok,
                            "--logs", "/var/log/nope.log", "--dir", str(tmp_path / "agent"), "--no-service"], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (tmp_path / "agent" / "agent.py").exists() and (tmp_path / "agent" / "spool").is_dir()
        env = (tmp_path / "agent" / "agent.env").read_text()
        assert f"WATCHOVER_TOKEN='{tok}'" in env and "WATCHOVER_METRICS=1" in env and oct((tmp_path / "agent" / "agent.env").stat().st_mode)[-3:] == "600"
        assert reg.get(rec["id"])["events"] >= 1 and store.snapshot()[-1].attributes["agent"] == "app-03"        # the self-test hello arrived
        r2 = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "agent-install.sh"), "--url", f"http://127.0.0.1:{port}/ingest", "--token", "wo_wrong",
                             "--dir", str(tmp_path / "agent2"), "--no-service"], capture_output=True, text=True, timeout=120)
        assert r2.returncode != 0 and "token" in (r2.stdout + r2.stderr).lower()
        r3 = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "agent-install.sh"), "--dir", str(tmp_path / "agent"), "--uninstall"], capture_output=True, text=True, timeout=60)
        assert r3.returncode == 0 and not (tmp_path / "agent").exists()
    finally:
        srv.shutdown()


def _post(port, body, tok=None, name="a.jsonl", agent="x"):
    h = {"X-Agent": agent, "X-File-Name": name}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/ingest", data=body, headers=h, method="POST"), timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:                                                      # 413 arrives before the body is read: the socket may reset first
        return 413


def test_receiver_fails_closed_and_caps_body(tmp_path):
    from watchover import live
    kb = Knowledge(str(tmp_path / "k.db")); reg = AgentRegistry(kb); store = LiveStore()
    srv = start_receiver(store, 0, None, reg); port = srv.server_address[1]
    line = b'{"level":"error","msg":"x","host":"test-db-01"}\n'
    try:
        assert _post(port, line) == 200                                  # open mode: nothing enrolled yet
        rec, tok = reg.enroll("db-01", "prod")
        reg._active = None
        assert _post(port, line) == 401                                  # first agent enrolled: token-less senders are refused
        assert _post(port, line, tok) == 200
        assert store.buf[-1].environment == "prod"                       # registered env beats the "test" guessed from the host name
        assert _post(port, b"", tok) == 400
        assert _post(port, b"x" * (live.MAX_BODY + 1), tok) == 413
    finally:
        srv.shutdown(); srv.server_close()


def test_agent_tail_rotation_truncation_partial(tmp_path):
    import sys, threading, time
    sys.path.insert(0, str(ROOT)); import agent
    log = tmp_path / "app.log"; log.write_text("old line\n")
    sent = []

    class S:
        def send(self, data, name):
            sent.append(data.decode())
    stop = threading.Event()
    th = threading.Thread(target=agent.tail_loop, args=(str(log), S(), 0.1, stop), daemon=True); th.start()
    time.sleep(0.6)
    with log.open("a") as f:
        f.write("first\nhalf"); f.flush()
    time.sleep(0.6)
    assert "".join(sent) == "first\n"                                    # the unfinished line waits for its newline
    with log.open("a") as f:
        f.write(" done\n")
    time.sleep(0.6)
    assert "".join(sent) == "first\nhalf done\n"
    log.rename(tmp_path / "app.log.1"); log.write_text("after rotate\n")  # logrotate style: rename + new file
    time.sleep(1.0)
    assert "after rotate\n" in "".join(sent)
    log.write_text("")                                                   # copytruncate style: same inode, shrinks
    time.sleep(0.5)                                                      # (a poller can only see a shrink that lasts a tick, like tail -F)
    with log.open("a") as f:
        f.write("after truncate\n")
    time.sleep(1.0)
    stop.set(); th.join(2)
    assert "after truncate\n" in "".join(sent)


def test_agent_sender_drops_permanent_errors(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT)); import agent
    store = LiveStore(); srv = start_receiver(store, 0, None); port = srv.server_address[1]
    try:
        s = agent.Sender(f"http://127.0.0.1:{port}/ingest", None, "t", str(tmp_path / "spool"))
        assert s.send(b"", "x.jsonl") is None and s.dropped == 1 and not list((tmp_path / "spool").glob("*"))   # HTTP 400: dropped, not spooled
        assert oct((tmp_path / "spool").stat().st_mode)[-3:] == "700"
    finally:
        srv.shutdown(); srv.server_close()


def test_self_enrolment(tmp_path):
    """A server presents the fleet key to /enroll and gets its own token; wrong key 401, duplicate name 409, disabled key 401."""
    import subprocess, shutil
    kb = Knowledge(str(tmp_path / "k.db")); reg = AgentRegistry(kb)
    assert reg.enroll_key(create=False) == ""
    key = reg.enroll_key(); assert key.startswith("wk_") and reg.enroll_key() == key
    store = LiveStore(); srv = start_receiver(store, 0, None, reg); port = srv.server_address[1]

    def enroll(k, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/enroll", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {k}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}
    try:
        code, out = enroll(key, {"name": "web-07", "env": "PROD", "site": "IST"})
        assert code == 200 and out["token"].startswith("wo_") and out["env"] == "prod"
        assert reg.verify(out["token"])["name"] == "web-07" and reg.get(reg.verify(out["token"])["id"])["note"] == "self-enrolled"
        assert enroll(key, {"name": "web-07"})[0] == 409                     # already enrolled: rotate instead
        assert enroll("wk_wrong", {"name": "x"})[0] == 401
        assert _post(port, b'{"msg":"hi"}\n', out["token"]) == 200              # the issued token ingests
        assert _post(port, b'{"msg":"hi"}\n', key) == 401                     # the fleet key itself never ingests
        if shutil.which("bash"):                                             # installer path: --enroll-key issues and stores the token
            r = subprocess.run(["bash", str(ROOT / "scripts" / "agent-install.sh"), "--url", f"http://127.0.0.1:{port}/ingest", "--enroll-key", key,
                                "--name", "app-09", "--env", "staging", "--dir", str(tmp_path / "a9"), "--no-service"], capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stdout + r.stderr
            assert "WATCHOVER_TOKEN='wo_" in (tmp_path / "a9" / "agent.env").read_text() and reg.verify(None) is None
            assert [a["name"] for a in reg.list() if a["name"] == "app-09"]
        reg.disable_enroll_key()
        assert enroll(key, {"name": "y"})[0] == 401 and reg.enroll_key(create=False) == ""
    finally:
        srv.shutdown(); srv.server_close()



def test_agent_discovery_and_file_stats(tmp_path, monkeypatch):
    """--discover finds application logs by profile; the live store reports per-file error stats and discovery events."""
    import sys
    sys.path.insert(0, str(ROOT)); import agent
    sap = tmp_path / "usr" / "sap" / "TPD" / "D00" / "work"; sap.mkdir(parents=True)
    (sap / "dev_w0").write_text("x"); (sap / "dev_disp").write_text("x"); (sap / "available.log").write_text("x")
    ora = tmp_path / "u01" / "app" / "oracle" / "diag" / "rdbms" / "orcl" / "orcl" / "trace"; ora.mkdir(parents=True); (ora / "alert_orcl.log").write_text("x")
    monkeypatch.setattr(agent, "PROFILES", [(n, [str(tmp_path) + m for m in ms], [str(tmp_path) + g for g in gs]) for n, ms, gs in agent.PROFILES if n in ("SAP NetWeaver ABAP", "Oracle Database", "nginx")])
    found = agent.discover_logs()
    assert [f["app"] for f in found] == ["SAP NetWeaver ABAP", "Oracle Database"]
    assert sum(len(f["files"]) for f in found) == 4 and any(p.endswith("alert_orcl.log") for p in found[1]["files"])
    store = LiveStore(); srv = start_receiver(store, 0, None); port = srv.server_address[1]
    try:
        s = agent.Sender(f"http://127.0.0.1:{port}/ingest", None, "sap-01")
        agent.discovery_report(s, "prod", found)
        s.send(b'{"level":"error","msg":"tp fail","host":"sap-01"}\n{"level":"info","msg":"ok","host":"sap-01"}\n', "dev_w0")
        s.send(b'{"level":"info","msg":"fine","host":"sap-01"}\n', "available.log")
        disc = store.discoveries()
        assert set(a["app"] for a in disc["sap-01"]) == {"SAP NetWeaver ABAP", "Oracle Database"} and len(disc["sap-01"][1]["files"]) == 3
        files = store.files(15)
        assert files[0]["file"] == "dev_w0" and files[0]["errors"] == 1 and files[0]["total"] == 2 and files[0]["last_msg"] == "tp fail"
        assert [f["file"] for f in files] == ["dev_w0", "available.log"] and all(f["file"] != "discovery.jsonl" for f in files)
        assert [o.message for o in store.file_lines("sap-01", "dev_w0", errors_only=True)] == ["tp fail"]
    finally:
        srv.shutdown(); srv.server_close()
