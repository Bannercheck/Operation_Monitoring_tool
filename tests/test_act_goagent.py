"""The Go host agent's actuator role (goagent/, `wo-agent act`) against the dashboard's real job channel, in simulator mode
(docs/design/action-agent.md §9.3; WP11b / WP11c), plus the shared parity fixtures, the RFC 8032 vectors on the Python side,
the one-command enrolment that issues both tokens and the installer's argument checks.
The Go tests need a Go toolchain and are skipped without one, like tests/test_go_receiver.py."""
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from watchover.act import keyd as keydmod
from watchover.act.jobs import attest_ok, ed25519_verify
from watchover.act.keyd import KeyStore

ROOT = Path(__file__).resolve().parents[1]
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
sys.path.insert(0, str(ROOT / "tests"))
import act_parity  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- shared fixtures and vectors (no Go needed)
def test_parity_fixtures_are_current():
    """tests/act_parity.py wrote them; the Go tests read them. A validator change must regenerate both."""
    assert (ROOT / "tests/fixtures/act_canonical.jsonl").read_text(encoding="utf-8") == act_parity.dump(act_parity.canonical_rows())
    assert (ROOT / "tests/fixtures/act_params_parity.jsonl").read_text(encoding="utf-8") == act_parity.dump(act_parity.param_rows())


def test_rfc8032_vectors_python_side():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    d = json.loads((ROOT / "tests/fixtures/rfc8032_ed25519.json").read_text())
    for v in d["vectors"]:
        k = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v["secret"]))
        msg = bytes.fromhex(v["message"])
        assert k.sign(msg).hex() == v["signature"], v["name"]
        pub = "ed25519:" + base64.b64encode(bytes.fromhex(v["public"])).decode()
        sig = base64.b64encode(bytes.fromhex(v["signature"])).decode()
        assert ed25519_verify(pub, sig, msg) and not ed25519_verify(pub, sig, msg + b"x")


@pytest.mark.parametrize("args, why", [
    (["--actions-only", "--url", "http://127.0.0.1:1"], "actions-token"),
    (["--actions-only", "--url", "http://127.0.0.1:1", "--actions-token", "wo_x"], "wa_"),
    (["--actions-only", "--url", "http://127.0.0.1:1", "--actions-token", "wa_x", "--agent-sha256", "zz"], "64 lowercase hex"),
    (["--actions-only", "--url", "http://127.0.0.1:1", "--actions-token", "wa_x", "--actions-max-risk", "R9"], "R0..R4"),
])
def test_installer_refuses_bad_actuator_arguments(args, why):
    r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), *args], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and why in (r.stdout + r.stderr)


# ---------------------------------------------------------------- the app with keyd and an in-process receiver
@pytest.fixture
def keyd(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="wg"))
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
def app(tmp_path, monkeypatch, keyd):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.live import start_receiver
    a = create_app()
    svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        port = free_port()
        srv = start_receiver(svc.live, port, None, svc.agents, None, svc.act.jobs)
        cl.base = f"http://127.0.0.1:{port}"
        yield cl
        srv.shutdown()
        srv.server_close()


def enroll(app, name):
    key = app.svc.agents.enroll_key()
    req = urllib.request.Request(app.base + "/enroll", data=json.dumps({"name": name, "env": "prod"}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_enrolment_issues_both_tokens_and_the_pins_dormant_while_off(app):
    app.svc.act.keys.policy.set("act_key_custody", "keyd", by="admin@x")
    app.svc.act.keys.ensure_keys()
    out = enroll(app, "web-prod-07")
    assert out["token"].startswith("wo_") and out["actuator_token"].startswith("wa_")
    pubs = app.svc.act.keys.public()
    assert out["dispatch_pub"] == pubs["dispatch"] and out["anchor_pub"] == pubs["anchor"]
    rec = app.svc.act.jobs.actuators.by_token(out["actuator_token"])
    assert rec and rec["host"] == "web-prod-07" and rec["key_status"] == "none"
    assert app.svc.agents.verify(out["actuator_token"]) is None                     # a wa_ token never ingests
    # act_mode is off: the channel answers, polls stay dormant
    from watchover.act.fake_actuator import FakeActuator, direct
    fa = FakeActuator(out["actuator_token"], "web-prod-07", direct(app.svc.act.jobs))
    code, _h, body = fa.hello()
    assert code == 200 and body["dormant"] is True and body["agent_id"] == rec["agent_id"] and "keys" not in body
    app.svc.act.set_mode("guided", by="admin@watchover.local")
    code, _h, body = fa.hello()
    assert body["dormant"] is False and body["keys"]["dispatch"] == pubs["dispatch"]


# ---------------------------------------------------------------- the real Go binary in simulator mode
@pytest.fixture(scope="module")
def wo_agent(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    out = tmp_path_factory.mktemp("bin") / "wo-agent"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "goagent", check=True, capture_output=True, timeout=300)
    return out


def sandbox(tmp_path, binary, out):
    sim = tmp_path / "sandbox"
    for d in ("var/log/app", ".sim", "etc/watchover", "usr/lib/watchover/actions"):
        (sim / d).mkdir(parents=True, exist_ok=True)
    (sim / ".sim" / "disk.json").write_text(json.dumps({"size_bytes": 1000000, "base_used_bytes": 930000}))
    (sim / "var/log/app/orders.log").write_text("x" * 1000)
    state = tmp_path / "state"
    tok = tmp_path / "token"
    tok.write_text(out["actuator_token"] + "\n")
    args = ["--actions-dir", str(sim / "etc/watchover/actions.d"), "--state-dir", str(state), "--scripts-dir", str(sim / "usr/lib/watchover/actions")]
    r = subprocess.run([str(binary), "act", "install", "--sim", "--packs", "linux-diag,linux-act", "--max-risk", "R1",
                        "--dispatch-pub", out["dispatch_pub"], "--anchor-pub", out["anchor_pub"], "--token-file", str(tok), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return sim, state, args


def once(binary, base, sim, args, host="web-prod-07"):
    r = subprocess.run([str(binary), "act", "--once", "--url", base, "--host", host, "--sim", str(sim),
                        "--halt-file", str(sim / "etc/watchover/actions.halt"), *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stderr


def test_go_actuator_round_trip_in_simulator_mode(app, wo_agent, tmp_path):
    act = app.svc.act
    act.keys.policy.set("act_key_custody", "keyd", by="admin@x")
    act.set_mode("guided", by="admin@watchover.local")
    out = enroll(app, "web-prod-07")
    sim, state, args = sandbox(tmp_path, wo_agent, out)
    once(wo_agent, app.base, sim, args)                                                # hello: the key is registered, not confirmed
    rec = act.jobs.actuators.by_token(out["actuator_token"])
    act.jobs.actuators.invalidate()
    rec = act.jobs.actuators.get(rec["agent_id"])
    fp = subprocess.run([str(wo_agent), "act", "fingerprint", "--state-dir", str(state)], capture_output=True, text=True).stdout.strip()
    assert rec["key_status"] == "unconfirmed" and rec["fingerprint"] == fp and rec["allowlist_ok"] == 1
    tpl = {t["id"]: t for t in json.loads(rec["templates"])}
    cat = act.catalogue.get("linux.disk.usage")
    assert tpl["linux.disk.usage"]["sha256"] == cat["sha256"]                         # the Go semantic hash equals the catalogue's
    assert json.loads(rec["policy"])["max_risk"] == "R1"
    act.jobs.actuators.confirm_key(rec["agent_id"], fp, "admin@watchover.local")
    j = act.jobs.dispatch(host="web-prod-07", template_id="linux.disk.usage", template_sha256=cat["sha256"], params={"path": "/var/log"},
                          mode="diagnose")
    log = once(wo_agent, app.base, sim, args)
    done = act.jobs.get(j["job_id"])
    assert done["state"] == "done" and done["result_sig_ok"] == 1, log
    res = json.loads(done["result"])
    assert res["fields"]["mount"] == "/" and float(res["fields"]["use_pct"]) >= 93.0 and done["ledger_n"] == 1
    issued = {r["envelope_sha256"] for r in act.db._exec("SELECT envelope_sha256 FROM act_jobs")}
    line = json.loads((state / "ledger.jsonl").read_text().splitlines()[0])
    assert line["envelope_sha256"] in issued                                          # the ledger binds the envelope byte for byte
    # a job for a template the host does not allow (risk above local max_risk R1) is refused by the actuator
    rst = act.catalogue.get("linux.service.restart")
    j2 = act.jobs.dispatch(host="web-prod-07", template_id="linux.service.restart", template_sha256=rst["sha256"],
                           params={"unit": "orders.service"}, mode="execute")
    once(wo_agent, app.base, sim, args)
    assert act.jobs.get(j2["job_id"])["reject_code"] == "risk_policy" and not act.db._exec("SELECT 1 FROM act_host_locks")
    # an anchor reaches the actuator, which verifies it with the pinned anchor.pub and counter-signs it in the next hello
    a = act.anchors.anchor("manual")
    assert a is not None
    once(wo_agent, app.base, sim, args)
    once(wo_agent, app.base, sim, args)
    att = act.db._exec("SELECT * FROM act_anchor_attest")
    assert att and att[0]["anchor_seq"] == a["seq"]
    pub = act.jobs.actuators.get(rec["agent_id"])["pubkey"]
    assert attest_ok(pub, {"seq": a["seq"], "mac": a["mac"], "ts": a["ts"], "sig": att[0]["sig"]}, "web-prod-07")
    v = act.audit.verify()
    assert v["ok"] and v["anchors"]["attest_sig_ok"] >= 1, v


def test_go_actuator_runs_the_engines_diagnose_step(app, wo_agent, tmp_path):
    """A run of the starter /var/log runbook: d1 goes to the Go actuator, its signed result becomes the step's capture."""
    import copy
    from watchover.act.importer import starter_bodies
    from watchover.act.types import Trigger
    act = app.svc.act
    act.keys.policy.set("act_key_custody", "keyd", by="admin@x")
    act.set_mode("guided", by="admin@watchover.local")
    for email in ("a@corp.com", "b@corp.com"):
        app.svc.users.register(email, "Str0ng!Passw0rd", name=email[0], role="viewer")
    body = copy.deepcopy(starter_bodies()["linux-varlog-full"])
    act.runbooks.create("varlog-team", body["title"], None, body, "a@corp.com")
    act.runbooks.submit("varlog-team", 1, "a@corp.com", tabletop=True)
    act.runbooks.review("varlog-team", 1, "b@corp.com", "approve")
    out = enroll(app, "web-prod-07")
    sim, state, args = sandbox(tmp_path, wo_agent, out)
    once(wo_agent, app.base, sim, args)
    rec = act.jobs.actuators.get(act.jobs.actuators.by_token(out["actuator_token"])["agent_id"])
    act.jobs.actuators.confirm_key(rec["agent_id"], rec["fingerprint"], "admin@watchover.local")
    once(wo_agent, app.base, sim, args)                                                # a fresh hello: polls start
    cid = act.cases.route(Trigger(source="threshold", key="threshold:web-prod-07:disk", host="web-prod-07", env="prod", severity="high",
                                  title="Disk dolu", facts={"metric": "disk_pct", "value": 93},
                                  evidence=[{"kind": "pasted", "host": "web-prod-07", "text": "No space left on device (/var/log/app/x.log)"}]))["case_id"]
    run = act.engine.start(cid, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    d1 = next(s for s in run["steps"] if s["step_key"] == "d1")
    assert d1["status"] == "dispatched", d1
    once(wo_agent, app.base, sim, args)
    run = act.engine.advance(run["id"])
    d1 = next(s for s in run["steps"] if s["step_key"] == "d1")
    assert d1["status"] == "succeeded" and d1["signed"] == 1 and d1["captures"]["mount"] == "/"
    assert any(e["source"] == "trusted_signed" for e in act.evidence.items(cid))       # signed diagnose evidence of the run
