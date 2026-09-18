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
