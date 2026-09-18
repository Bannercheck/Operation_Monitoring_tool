"""Agent identity: enrol / verify / revoke / rotate, the receiver stamps events with the registered identity, settings persist."""
import json
import urllib.error
import urllib.request

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
        assert f"WATCHOVER_TOKEN={tok}" in env and "WATCHOVER_METRICS=1" in env and oct((tmp_path / "agent" / "agent.env").stat().st_mode)[-3:] == "600"
        assert reg.get(rec["id"])["events"] >= 1 and store.snapshot()[-1].attributes["agent"] == "app-03"        # the self-test hello arrived
        r2 = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "agent-install.sh"), "--url", f"http://127.0.0.1:{port}/ingest", "--token", "wo_wrong",
                             "--dir", str(tmp_path / "agent2"), "--no-service"], capture_output=True, text=True, timeout=120)
        assert r2.returncode != 0 and "token" in (r2.stdout + r2.stderr).lower()
        r3 = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "scripts" / "agent-install.sh"), "--dir", str(tmp_path / "agent"), "--uninstall"], capture_output=True, text=True, timeout=60)
        assert r3.returncode == 0 and not (tmp_path / "agent").exists()
    finally:
        srv.shutdown()
