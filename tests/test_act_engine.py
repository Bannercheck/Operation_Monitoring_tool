"""WP8: the run engine (docs/design/action-agent.md §6, §9.7, §10.3–§10.6; C22, C48, C49, R7, R8, I7, I13) with a fake clock.

Guided runs need no actuator; system-executed steps go through the real job channel to the fake actuator (act/fake_actuator.py)
with a real keyd, so dispatch, host lock, signed results and evidence are the production code paths."""
import copy
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from watchover.act import keyd as keydmod
from watchover.act.engine import Engine, EngineError
from watchover.act.fake_actuator import FakeActuator, direct
from watchover.act.importer import starter_bodies
from watchover.act.keyd import KeyStore
from watchover.act.types import Trigger, cell_key

HOST = "web-prod-07"
NOSPACE = "ERROR orders write failed: No space left on device (/var/log/app/orders.log)"


def df(pct):
    size = 52403200
    used = int(size * pct / 100)
    return f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 {size} {used} {size - used} {int(pct)}% /\n"


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t

    def tick(self, **kw):
        self.t += timedelta(**kw)


class FakeSources:
    """What verify and watch read, set by the test."""
    def __init__(self):
        self.metrics, self.logs, self.err, self.cases, self.silent, self.slos = {}, 0, None, [], 0.0, (None, None)

    def metric(self, host, metric, within_s=300):
        return self.metrics.get(metric)

    def log_count(self, phrases, host, window_s, at):
        return self.logs

    def http_status(self, url, timeout=5.0):
        return 200

    def anomaly_open(self, host, key):
        return None

    def bizflow_stuck(self, flow, step):
        return None

    def slo(self, host, window_min=15, at=None):
        return self.slos[0] if window_min >= 60 else self.slos[1]

    def errors(self, host, start, end):
        return self.err

    def templates_seen(self, host, start, end):
        return set()

    def new_cases(self, host, since, exclude_key):
        return list(self.cases)

    def agent_last_seen(self, host):
        return None


@pytest.fixture
def keyd(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="we"))
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
    monkeypatch.delenv("WATCHOVER_ACT_DEMO", raising=False)
    monkeypatch.delenv("WATCHOVER_ACT_TIME_SCALE", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app()
    svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        act = svc.act
        act.set_mode("guided", by="admin@watchover.local")
        for email in ("a@corp.com", "b@corp.com"):
            svc.users.register(email, "Str0ng!Passw0rd", name=email[0], role="viewer")
        cl.clock = Clock()
        cl.src = FakeSources()
        act.engine = Engine(act, clock=cl.clock, sources=cl.src)
        cl.eng = act.engine
        yield cl


def publish(act, key, body):
    act.runbooks.create(key, body.get("title", key), None, body, "a@corp.com")
    act.runbooks.submit(key, 1, "a@corp.com", tabletop=True)
    act.runbooks.review(key, 1, "b@corp.com", "approve")


def varlog(act, key="varlog-team", mutate=None):
    body = copy.deepcopy(starter_bodies()["linux-varlog-full"])
    if mutate:
        mutate(body)
    publish(act, key, body)
    return key


def restart_rb(act, key="svc-restart", mutate=None):
    body = {"key": key, "version": 1, "title": "Servisi yeniden başlat", "risk": "R2",
            "match": {"sources": ["threshold"], "templates": ["*orders*"]}, "applies_to": {"os": ["linux"]},
            "params": {"unit": {"type": "unit", "from": ["default"], "default": "orders.service"}},
            "steps": [{"id": "a1", "type": "act", "title": "Yeniden başlat", "template": "linux.service.restart", "args": {"unit": "{unit}"},
                       "verify": "v1"},
                      {"id": "v1", "type": "verify", "title": "Hata oranı düştü mü?",
                       "checks": [{"metric_below": {"metric": "errors", "value": 5, "within_s": 300}},
                                  {"http_ok": {"url": "http://127.0.0.1:9/health", "probe_from": "server"}, "independent": True}],
                       "require": "all", "retries": 2, "interval_s": 30}],
            "watch": {"minutes": 15, "expected_noise_s": 0, "expected_noise": []}}
    if mutate:
        mutate(body)
    publish(act, key, body)
    return key


def case(c, host=HOST, key=None):
    return c.svc.act.cases.route(Trigger(source="threshold", key=key or f"threshold:{host}:disk", host=host, env="prod", severity="high",
                                         title="Disk dolu: /var/log", facts={"metric": "disk_pct", "value": 93, "limit": 90},
                                         evidence=[{"kind": "pasted", "host": host, "text": NOSPACE}]))["case_id"]


def step(run, key):
    return next(s for s in run["steps"] if s["step_key"] == key)


def guided_until_act(c, rid, d1_pct=93):
    e = c.eng
    run = e.get(rid)
    e.done(rid, step(run, "d1")["id"], "a@corp.com", "ok", output=df(d1_pct))
    e.done(rid, step(e.get(rid), "d2")["id"], "a@corp.com", "ok", output="7812345678 /var/log/app\n")
    e.done(rid, step(e.get(rid), "d3")["id"], "a@corp.com", "ok", output="deleted_open_bytes=0 files=0")
    return e.choose(rid, step(e.get(rid), "c1")["id"], "a@corp.com", "a1")


def exec_events(c, rid):
    return c.svc.act.db._exec("SELECT step_key, outcome FROM act_trust_events WHERE run_id=? AND component='exec' ORDER BY step_key", (rid,))


# ---------------------------------------------------------------- guided path
def test_guided_path_new_to_resolved(c):
    act, e = c.svc.act, c.eng
    varlog(act)
    cid = case(c)
    run = e.start(cid, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    assert run["status"] == "running" and step(run, "d1")["status"] == "guided" and "df -P" in step(run, "d1")["human_cmd"]
    assert act.cases.get(cid)["phase"] == "waiting_human"
    run = guided_until_act(c, run["id"])
    a1 = step(run, "a1")
    assert a1["status"] == "guided" and "gzip" in a1["human_cmd"] and step(run, "a2")["status"] == "skipped"
    c.src.metrics["disk"] = 62
    run = e.done(run["id"], a1["id"], "a@corp.com", "ok", note="sıkıştırdım")
    v1 = step(run, "v1")
    assert v1["status"] == "guided" and v1["reason"] == "paste"                     # no actuator: the template check is a paste box
    run = e.done(run["id"], v1["id"], "a@corp.com", "ok", output=df(62))
    assert run["status"] == "watching" and act.cases.get(cid)["phase"] == "watching"
    assert step(run, "r1")["status"] == "skipped" and step(run, "m1")["status"] == "skipped"
    c.clock.tick(minutes=14)
    assert e.advance(run["id"])["status"] == "watching"
    c.clock.tick(minutes=2)
    run = e.advance(run["id"])
    assert run["status"] == "completed_ok" and run["label"] == "ok"
    assert act.cases.get(cid)["phase"] == "resolved" and act.cases.get(cid)["label"] == "ok"
    ev = exec_events(c, run["id"])
    assert sorted(r["step_key"] for r in ev) == ["a1", "d1", "d2", "d3"]           # exactly one exec event per step; none for verify
    assert {r["step_key"]: r["outcome"] for r in ev}["a1"] == "ok"
    assert not act.db._exec("SELECT 1 FROM act_host_locks")


def test_claim_vs_verify_and_transient_failure_escalates(c):
    act, e = c.svc.act, c.eng
    restart_rb(act)
    cid = case(c, key="threshold:web-prod-07:orders")
    run = e.start(cid, "a@corp.com", "svc-restart")
    a1 = step(run, "a1")
    assert a1["status"] == "guided" and a1["human_cmd"] == "sudo -n /usr/bin/systemctl restart orders.service"
    c.src.metrics["errors"] = 40                                                    # the person says it worked; the data says no
    run = e.done(run["id"], a1["id"], "a@corp.com", "ok")
    assert step(run, "v1")["status"] == "running"                                  # first attempt failed, retry scheduled
    c.clock.tick(seconds=31)
    run = e.advance(run["id"])
    a1 = step(run, "a1")
    assert run["status"] == "failed" and a1["human_claim"]["result"] == "ok" and a1["outcome"] == "verify_failed"
    assert act.cases.get(cid)["phase"] == "escalated"                              # transient: no rollback, escalation
    assert [r["outcome"] for r in exec_events(c, run["id"])] == ["verify_failed"]


def test_unverified_when_no_check_has_data(c):
    act, e = c.svc.act, c.eng
    restart_rb(act, mutate=lambda b: b["steps"][1].update({"checks": [{"metric_below": {"metric": "errors", "value": 5}}], "retries": 1}))
    cid = case(c, key="threshold:web-prod-07:orders")
    run = e.start(cid, "a@corp.com", "svc-restart")
    run = e.done(run["id"], step(run, "a1")["id"], "a@corp.com", "ok")
    assert run["status"] == "watching"
    c.clock.tick(minutes=16)
    run = e.advance(run["id"])
    assert step(run, "a1")["outcome"] == "unverified" and run["label"] == "unverified"


# ---------------------------------------------------------------- preconditions, windows, freeze
def test_precondition_else_modes(c):
    act, e = c.svc.act, c.eng
    varlog(act)
    cid = case(c)
    run = e.start(cid, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    run = guided_until_act(c, run["id"], d1_pct=70)                               # fact "steps.d1.use_pct >= 85" asks
    assert run["status"] == "blocked" and run["state"]["blocked"]["check"] == "fact" and run["state"]["blocked"]["mode"] == "ask"
    run = e.proceed(run["id"], "a@corp.com")
    assert run["status"] == "running" and step(run, "a1")["status"] == "guided"
    # stop: another run holds the host
    cid2 = act.cases.manual("Elle açılan vaka", HOST, "high", None, None, "b@corp.com")["case_id"]
    r2 = e.start(cid2, "b@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    assert r2["status"] == "blocked" and r2["state"]["blocked"]["mode"] == "stop" and str(run["id"]) in r2["state"]["blocked"]["reason"]
    with pytest.raises(EngineError):
        e.start(cid2, "b@corp.com", "varlog-team")                                  # one active run per case
    # guided: no actuator online → every act step is guided (the precondition said so)
    assert step(run, "a1")["mode"] == 1


def test_wait_for_window_then_go(c):
    act, e = c.svc.act, c.eng
    restart_rb(act, mutate=lambda b: b.update({"preconditions": [{"check": "in_window", "window": "gece", "else": "wait"}]}))
    cid = case(c, key="threshold:web-prod-07:orders")
    run = e.start(cid, "a@corp.com", "svc-restart")
    assert run["status"] == "blocked" and run["state"]["blocked"]["mode"] == "wait"
    act.db._exec("INSERT INTO act_windows (team_id, name, kind, env, selector, rule, enabled) VALUES (NULL,'gece','window','','{}',?,1)",
                 (json.dumps({"tz": "UTC", "days": [1, 2, 3, 4, 5, 6, 7], "from": "00:00", "to": "24:00"}),))
    run = e.advance(run["id"])
    assert run["status"] == "running" and step(run, "a1")["status"] == "guided"


def test_freeze_caps_act_steps_at_guided(c, monkeypatch):
    act, e = c.svc.act, c.eng
    restart_rb(act)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    act.db._exec("INSERT INTO act_windows (team_id, name, kind, env, selector, rule, enabled) VALUES (NULL,'yıl sonu','freeze','prod','{}',?,1)",
                 (json.dumps({"tz": "UTC", "dates": [{"from": today, "to": today}]}),))
    cid = case(c, key="threshold:web-prod-07:orders")
    run = e.start(cid, "a@corp.com", "svc-restart")
    a1 = step(run, "a1")
    assert a1["status"] == "guided" and "freeze" in a1["reason"]


# ---------------------------------------------------------------- system execution through the job channel
def system_rig(c, rollback_cell=True):
    """approve mode, a confirmed fake actuator that reports the templates, cells at L2 for a1 / r1."""
    act = c.svc.act
    act.policy.set("act_residual_risk_accepted", "2026-10-15T00:00:00+00:00", by="admin@x")
    act.set_mode("approve", by="admin@watchover.local")
    rec, _wo = c.svc.agents.enroll(HOST)
    tok = act.jobs.actuators.issue(rec["id"], HOST, "admin@watchover.local")["token"]
    fa = FakeActuator(tok, HOST, direct(act.jobs), dispatch_pub=act.keys.public()["dispatch"], agent_id=rec["id"])
    tpl = [{"id": t["id"], "rev": t["rev"], "sha256": t["sha256"], "risk": t["risk"], "kind": t["kind"], "os": "linux"}
           for t in act.catalogue.list("approved") if t["os"] == "linux"]
    fa.hello(templates=tpl)
    act.jobs.actuators.confirm_key(rec["id"], fa.fingerprint, "admin@watchover.local")
    fa.signed = True
    fa.hello(templates=tpl)
    fa.cells = ("a1", "r1") if rollback_cell else ("a1",)
    return fa


def cells_l2(c, run, keys):
    for sk in keys:
        ck = cell_key(run.get("team_id"), "varlog-team", sk, run.get("env") or "", run.get("host_class") or "")
        c.svc.act.db._exec("INSERT INTO act_cells (cell_key, team_id, runbook_key, step_key, env, host_class, level, state) VALUES (?,?,?,?,?,?,?,?)",
                           (ck, run.get("team_id"), "varlog-team", sk, run.get("env") or "", run.get("host_class") or "", 2, "active"))


def pump(c, fa, rid, results, rounds=6):
    for _ in range(rounds):
        fa.run_once(results=results)
        run = c.eng.advance(rid)
    return run


def to_awaiting(c, fa, results):
    act, e = c.svc.act, c.eng
    varlog(act)
    cid = case(c)
    run = e.start(cid, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    cells_l2(c, run, fa.cells)
    assert step(run, "d1")["status"] == "dispatched" and step(run, "d1")["mode"] == 4      # light R0 diagnose: system, at once
    run = pump(c, fa, run["id"], results)
    assert step(run, "d1")["status"] == "succeeded" and step(run, "d1")["signed"] == 1 and step(run, "d1")["captures"]["use_pct"] == 93
    assert step(run, "c1")["status"] == "guided"
    run = e.choose(run["id"], step(run, "c1")["id"], "a@corp.com", "a1")
    run = pump(c, fa, run["id"], results)                                            # the dry run (read-only) first
    a1 = step(run, "a1")
    assert a1["status"] == "awaiting_approval", (a1["reason"], a1["mode"], run["state"])
    assert a1["mode"] == 2 and a1["dry_output"]["fields"]["files"] == 37 and a1["job_hash"]
    return cid, run


DIAG = {"linux.disk.usage": {"use_pct": 93, "mount": "/", "avail_gb": 3}, "linux.dir.top": {"rows": [{"path": "/var/log/app", "bytes": 7}]},
        "linux.fs.lsof_deleted": {"deleted_open_bytes": 0, "files": 0},
        "linux.logs.compress_old": {"files": 37, "bytes": 4402341888, "bytes_before": 4402341888, "bytes_after": 612368384, "list_id": "L1"},
        "linux.logs.decompress_list": {"files": 37}}


def test_verify_fail_rollback_is_one_rolled_back_outcome(c):
    act, e = c.svc.act, c.eng
    fa = system_rig(c)
    c.src.metrics["disk"] = 91
    cid, run = to_awaiting(c, fa, DIAG)
    a1 = step(run, "a1")
    with pytest.raises(EngineError):
        e.dispatch_approved(run["id"], a1["id"], "b@corp.com", job_hash="0" * 64)        # the approved plan must be the one shown
    run = e.dispatch_approved(run["id"], a1["id"], "b@corp.com", job_hash=a1["job_hash"])
    assert step(run, "a1")["status"] == "dispatched" and act.db._exec("SELECT host FROM act_host_locks")[0]["host"] == HOST
    run = pump(c, fa, run["id"], DIAG)                                               # executes; the verify job reports 93 %
    assert step(run, "a1")["status"] == "succeeded" and step(run, "v1")["status"] == "running"
    for _ in range(3):
        c.clock.tick(seconds=21)
        run = pump(c, fa, run["id"], DIAG)
    assert run["status"] == "rolling_back"
    r1 = step(run, "r1")
    assert r1["status"] == "awaiting_approval" and r1["params"] == {"list_id": "L1"}   # {steps.a1.out.list_id}
    run = e.dispatch_approved(run["id"], r1["id"], "b@corp.com", job_hash=r1["job_hash"])
    c.src.metrics["disk"] = 70
    results = dict(DIAG, **{"linux.disk.usage": {"use_pct": 70, "mount": "/"}})
    run = pump(c, fa, run["id"], results)
    assert run["status"] == "rolled_back", [(s["step_key"], s["status"]) for s in run["steps"]]
    assert step(run, "a1")["post_rollback_verify"]["ok"] is True
    outs = {r["step_key"]: r["outcome"] for r in exec_events(c, run["id"])}
    assert outs["a1"] == "rolled_back" and outs["r1"] == "ok"                       # one outcome per step (C22)
    assert act.cases.get(cid)["phase"] == "rolled_back" and not act.kill.killed(host=HOST)


def test_failed_post_rollback_verify_is_rollback_failed_and_keeps_the_host_stop(c):
    act, e = c.svc.act, c.eng
    fa = system_rig(c)
    c.src.metrics["disk"] = 91
    cid, run = to_awaiting(c, fa, DIAG)
    run = e.dispatch_approved(run["id"], step(run, "a1")["id"], "b@corp.com")
    run = pump(c, fa, run["id"], DIAG)
    for _ in range(3):
        c.clock.tick(seconds=21)
        run = pump(c, fa, run["id"], DIAG)
    run = e.dispatch_approved(run["id"], step(run, "r1")["id"], "b@corp.com")
    run = pump(c, fa, run["id"], DIAG)                                               # still 93 % after the rollback
    for _ in range(3):
        c.clock.tick(seconds=21)
        run = pump(c, fa, run["id"], DIAG)
    assert run["status"] == "rollback_failed" and run["label"] == "rollback_failed"
    assert {r["step_key"]: r["outcome"] for r in exec_events(c, run["id"])}["a1"] == "rollback_failed"
    assert act.cases.get(cid)["phase"] == "escalated"
    k = act.kill.killed(host=HOST)
    assert k and k["scope"] == "host"                                               # kept until a person clears it


def test_kill_hard_cancels_in_flight_and_takeover(c):
    act, e = c.svc.act, c.eng
    fa = system_rig(c)
    _cid, run = to_awaiting(c, fa, DIAG)
    run = e.dispatch_approved(run["id"], step(run, "a1")["id"], "b@corp.com")
    job = step(run, "a1")["job_id"]
    assert act.jobs.get(job)["state"] == "queued"
    act.kill.stop("host", HOST, by="b@corp.com", reason="test", hard=True)
    run = e.advance(run["id"])
    a1 = step(run, "a1")
    assert a1["status"] == "guided" and a1["reason"] == "kill_hard" and act.jobs.get(job)["state"] == "cancelled"
    assert not act.db._exec("SELECT 1 FROM act_host_locks")
    # I7: a stopped host gets no new job, whatever the engine asks
    from watchover.act.jobs import ChannelError
    with pytest.raises(ChannelError):
        act.jobs.dispatch(host=HOST, template_id="linux.disk.usage", template_sha256="", params={"path": "/var"}, mode="diagnose")


def test_takeover_turns_an_approval_into_guided(c):
    act, e = c.svc.act, c.eng
    fa = system_rig(c)
    _cid, run = to_awaiting(c, fa, DIAG)
    run = e.takeover(run["id"], step(run, "a1")["id"], "a@corp.com")
    a1 = step(run, "a1")
    assert a1["status"] == "guided" and a1["reason"] == "takeover" and "gzip" in a1["human_cmd"]


def test_unknown_keeps_the_lock_until_a_person_decides(c):
    act, e = c.svc.act, c.eng
    fa = system_rig(c)
    c.src.metrics["disk"] = 60
    _cid, run = to_awaiting(c, fa, DIAG)
    run = e.dispatch_approved(run["id"], step(run, "a1")["id"], "b@corp.com")
    code, _h, body = fa.request("GET", f"/agent/v1/jobs?after={fa.last_seq}")        # leased, then the actuator goes silent
    assert code == 200 and body["jobs"]
    act.jobs.sweep(at=datetime.now(timezone.utc) + timedelta(hours=1))
    run = e.advance(run["id"])
    a1 = step(run, "a1")
    assert a1["status"] == "unknown" and a1["verify"]["probe"]["ok"] is True          # read-only verify: disk 60 %
    assert act.db._exec("SELECT host FROM act_host_locks")                          # the lock stays
    run = e.resolve_unknown(run["id"], a1["id"], "a@corp.com", "count_ok")
    assert step(run, "a1")["status"] == "succeeded" and not act.db._exec("SELECT 1 FROM act_host_locks")


# ---------------------------------------------------------------- demo controls and the API
def test_time_scale_and_demo_controls(c, monkeypatch):
    from watchover.act import demo
    from watchover.act.limits import time_scale
    monkeypatch.setenv("WATCHOVER_ACT_TIME_SCALE", "30")
    assert time_scale() == 1.0                                                      # refused without act_demo
    with pytest.raises(demo.DemoError):
        demo.disk(c.svc.act)
    monkeypatch.setenv("WATCHOVER_ACT_DEMO", "1")
    assert time_scale() == 30.0
    case_row = demo.disk(c.svc.act, reviewer="b@corp.com")
    assert case_row["phase"] == "ready" and case_row["host"] == "web-prod-07"
    run = demo.run_guided(c.svc.act, case_row["id"])
    assert run["status"] == "watching" and demo.disk_get("web-prod-07") == 62.0
    until = datetime.fromisoformat(run["watch_until"])
    assert (until - c.clock()).total_seconds() <= 31                              # 15 minutes at scale 30
    c.clock.tick(seconds=31)
    assert c.eng.advance(run["id"])["status"] == "completed_ok"


def test_api_runs_and_steps(c):
    act = c.svc.act
    varlog(act)
    cid = case(c)
    h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Watchover!Admin2026"}).json()["token"]}
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "varlog-team", "params": {"path": "/var/log", "days": 7}, "reason_code": "wrong_param"}, headers=h)
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    assert plan["runbook_key"] == "varlog-team" and [s["key"] for s in plan["steps"]][:2] == ["d1", "d2"]
    r = c.post(f"/api/act/cases/{cid}/runs", json={}, headers=h)
    assert r.status_code == 201, r.text
    run = r.json()
    d1 = step(run, "d1")
    r = c.post(f"/api/act/runs/{run['id']}/steps/{d1['id']}/done", json={"result": "ok", "output": df(93)}, headers=h)
    assert r.status_code == 200 and step(r.json(), "d1")["captures"]["use_pct"] == 93
    r = c.post(f"/api/act/runs/{run['id']}/steps/{d1['id']}/skip", json={"text": "x"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "act_step_state"
    assert c.get(f"/api/act/runs/{run['id']}", headers=h).json()["id"] == run["id"]
    r = c.post(f"/api/act/runs/{run['id']}/abort", json={"text": "test"}, headers=h)
    assert r.json()["status"] == "aborted" and act.cases.get(cid)["phase"] == "ready"


@pytest.mark.skipif(not __import__("os").environ.get("WATCHOVER_TEST_DATABASE_URL"), reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_postgresql_round(tmp_path, monkeypatch):
    """Stage 2b's SQL on PostgreSQL: a guided run end to end, an L2 act step through the job channel (host lock, signed result,
    captures), labels and exec events, the weekly metrics and a model answer on the frozen input."""
    import os
    from watchover.act import ActService, metrics, store
    from watchover.actions import open_store
    from watchover.db import Database
    from watchover.llm import LLMConfig
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_ACT_DEMO", "1")
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    db = Database(os.environ["WATCHOVER_TEST_DATABASE_URL"])
    for t in store.act_tables(db) + ["actions"]:
        db._exec(f"DROP TABLE IF EXISTS {t} CASCADE")
    if "rbac_meta" in db.tables():
        db._exec("DELETE FROM rbac_meta WHERE key LIKE 'act_%'")
    if "auth_events" in db.tables():
        db._exec("DELETE FROM auth_events WHERE event='act_anchor'")
    open_store(db, str(tmp_path / "none.db"))
    act = ActService(db=db)
    act.policy.set("act_key_custody", "embedded", by="a@x")
    act.policy.set("act_residual_risk_accepted", "2026-10-15T00:00:00+00:00", by="a@x")
    act.set_mode("approve", by="a@x")
    clock, src = Clock(), FakeSources()
    act.engine = Engine(act, clock=clock, sources=src)

    class Svc:                                            # what the act layer asks of the app: agent names, the act model
        agents = type("A", (), {"get": staticmethod(lambda aid: {"name": HOST} if int(aid) == 9001 else None)})()

        @staticmethod
        def llm_cfg(purpose="chat"):
            return LLMConfig("http://127.0.0.1:11434", "qwen-pg", "", provider="ollama")
    act.svc = Svc()
    e = act.engine
    varlog(act)
    cid = act.cases.route(Trigger(source="threshold", key=f"threshold:{HOST}:disk", host=HOST, env="prod", title="Disk",
                                  facts={"metric": "disk_pct"}, evidence=[{"kind": "pasted", "host": HOST, "text": NOSPACE}]))["case_id"]
    run = e.start(cid, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    run = guided_until_act(type("C", (), {"eng": e})(), run["id"])
    src.metrics["disk"] = 60
    run = e.done(run["id"], step(run, "a1")["id"], "a@corp.com", "ok")
    run = e.done(run["id"], step(run, "v1")["id"], "a@corp.com", "ok", output=df(60))
    assert run["status"] == "watching"
    clock.tick(minutes=16)
    run = e.advance(run["id"])
    assert run["status"] == "completed_ok" and run["label"] == "ok"
    assert db._exec("SELECT COUNT(*) AS n FROM act_trust_events WHERE run_id=? AND component='exec'", (run["id"],))[0]["n"] == 4
    # an L2 step through the job channel on PostgreSQL (embedded custody signs; the lock is taken FOR UPDATE)
    from watchover.act.fake_actuator import FakeActuator, direct
    rec_id = 9001
    tok = act.jobs.actuators.issue(rec_id, HOST, "a@x")["token"]
    fa = FakeActuator(tok, HOST, direct(act.jobs), dispatch_pub=act.keys.public()["dispatch"], agent_id=rec_id)
    tpl = [{"id": t["id"], "rev": t["rev"], "sha256": t["sha256"], "risk": t["risk"], "kind": t["kind"], "os": "linux"}
           for t in act.catalogue.list("approved") if t["os"] == "linux"]
    fa.hello(templates=tpl)
    act.jobs.actuators.confirm_key(rec_id, fa.fingerprint, "a@x")
    fa.signed = True
    fa.hello(templates=tpl)
    cid2 = act.cases.manual("Disk yine dolu", HOST, "high", None, None, "a@corp.com")["case_id"]
    clock.tick(minutes=40)
    run2 = e.start(cid2, "a@corp.com", "varlog-team", params={"path": "/var/log", "days": 7})
    cells_l2(type("C", (), {"svc": type("S", (), {"act": act})()})(), run2, ("a1",))
    run2 = pump(type("C", (), {"eng": e})(), fa, run2["id"], DIAG)
    assert step(run2, "d1")["status"] == "succeeded" and step(run2, "d1")["captures"]["use_pct"] == 93
    run2 = e.choose(run2["id"], step(run2, "c1")["id"], "a@corp.com", "a1")
    run2 = pump(type("C", (), {"eng": e})(), fa, run2["id"], DIAG)
    a1 = step(run2, "a1")
    assert a1["status"] == "awaiting_approval", (a1["reason"], a1["mode"])
    run2 = e.dispatch_approved(run2["id"], a1["id"], "b@corp.com", job_hash=a1["job_hash"])
    assert db._exec("SELECT host FROM act_host_locks")[0]["host"] == HOST
    run2 = pump(type("C", (), {"eng": e})(), fa, run2["id"], dict(DIAG, **{"linux.disk.usage": {"use_pct": 61, "mount": "/"}}))
    assert step(run2, "a1")["status"] == "succeeded" and run2["status"] == "watching", [(s["step_key"], s["status"]) for s in run2["steps"]]
    assert not db._exec("SELECT 1 AS x FROM act_host_locks")
    # metrics and a model answer on PostgreSQL
    db._exec("UPDATE act_decisions SET blind=1, peeked=0, decided_at=?", ("2026-10-14T10:00:00+00:00",))
    assert isinstance(metrics.weekly(db, "2026-W42"), list)
    monkeypatch.setattr("watchover.llm.ollama_version", lambda cfg: (0, 5, 0))
    cid3 = act.cases.route(Trigger(source="threshold", key="threshold:web-prod-09:disk", host="web-prod-09", env="prod", title="Disk",
                                   facts={"metric": "disk_pct"}, evidence=[{"kind": "pasted", "host": "web-prod-09", "text": NOSPACE}]))["case_id"]
    fi = act.shadow.frozen(cid3, 1)
    act.selector.call = lambda cfg, msgs, **k: json.dumps({"choice": "NONE", "params": {}, "cite": []})
    res = act.selector.select(cid3, 1)
    assert res["valid"] and fi["input_sha"] and db._exec("SELECT COUNT(*) AS n FROM act_model_cache")[0]["n"] == 1
    for t in store.act_tables(db) + ["actions"]:            # leave the shared test database as the other rounds expect it
        db._exec(f"DROP TABLE IF EXISTS {t} CASCADE")
    db._exec("DELETE FROM rbac_meta WHERE key LIKE 'act_%'")
    db.close()
