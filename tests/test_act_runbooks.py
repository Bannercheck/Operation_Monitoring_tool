"""WP5: runbooks (§6): identity rules (I6, administrators included), R3 publication, immutability, step-hash resets, lint
(expected_noise bounds, read-only freeze_exempt, independent checks, HTTP executor bindings), starter pack."""
import copy
import json

import pytest

from watchover.act import ActService, store
from watchover.act.importer import starter_bodies
from watchover.act.runbooks import RunbookError, errors, lint, step_hashes
from watchover.actions import open_store
from watchover.db import Database

ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def act(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k")
    db = Database(str(tmp_path / "k.db"))
    open_store(db, str(tmp_path / "none.db"))
    a = ActService(db=db)
    a.set_mode("guided", by="admin@x.com")
    return a


def varlog():
    return copy.deepcopy(starter_bodies()["linux-varlog-full"])


def codes(findings, level="error"):
    return {f["code"] for f in findings if f["level"] == level}


def test_starter_pack_lints_and_loads(act):
    tpl = act.catalogue.approved()
    for key, body in starter_bodies().items():
        assert errors(lint(body, tpl)) == [], key
    assert len(starter_bodies()) == 7
    rows = {r["key"]: r for r in act.runbooks.list()}
    assert set(rows) == set(starter_bodies()) and {r["status"] for r in rows.values()} == {"starter"}
    assert all(v["status"] == "draft" for r in rows.values() for v in r["versions"])
    assert act.ensure_starter() == 0                                       # once


def test_lint_rules(act):
    tpl = act.catalogue.approved()
    b = varlog()
    b["watch"] = {"minutes": 15, "expected_noise_s": 400,
                  "expected_noise": [{"template": "x*", "justification": "a"}] + [{"template": f"t{i}", "justification": ""} for i in range(5)]}
    for s in b["steps"]:
        if s["id"] == "a1":
            s["expect_silence_s"] = 500
    b["windows"]["freeze_exempt"] = True
    c = codes(lint(b, tpl))
    assert {"expected_noise", "expected_noise_s", "expect_silence_s", "freeze_exempt"} <= c
    diag = {"key": "disk-check", "title": "Disk", "risk": "R0", "params": {"path": {"type": "path", "roots": ["/var/log"], "from": ["default"],
                                                                               "default": "/var/log"}},
            "windows": {"freeze_exempt": True},
            "steps": [{"id": "d1", "type": "diagnose", "template": "linux.disk.usage", "args": {"path": "{path}"}}]}
    assert errors(lint(diag, tpl)) == []                                   # a read-only runbook may be freeze-exempt
    b = varlog()
    v1 = next(s for s in b["steps"] if s["id"] == "v1")
    v1["checks"][0]["independent"] = True                                   # the acting host's own actuator is never independent
    v1["checks"][3]["http_ok"]["probe_from"] = "host"
    c = codes(lint(b, tpl))
    assert "independent" in c
    b = varlog()
    v1 = next(s for s in b["steps"] if s["id"] == "v1")
    v1["checks"] = v1["checks"][:3]
    assert "no_independent_verify" in codes(lint(b, tpl), "warning")
    assert "no_independent_verify" not in codes(lint(varlog(), tpl), "warning")
    b = varlog()
    b["steps"].insert(6, {"id": "m9", "type": "manual", "title": "x", "text": "elle bir şey"})   # between a2 and v1
    assert "manual_before_verify" in codes(lint(b, tpl))
    b = varlog()
    a1 = next(s for s in b["steps"] if s["id"] == "a1")
    del a1["rollback"], a1["verify"]
    assert {"rollback_missing", "verify_missing"} <= codes(lint(b, tpl))
    b = varlog()
    b["params"]["days"] = {"type": "string", "from": ["ask"]}
    b["params"]["p"] = {"type": "pid", "pid_exe": "java", "from": ["evidence.pid"]}
    b["params"]["q"] = {"type": "int", "min": 1, "max": 3}
    c = codes(lint(b, tpl))
    assert {"param_type", "pid_source", "param_source"} <= c
    b = varlog()
    b["steps"][0]["template"] = "linux.nope"
    b["steps"][1]["args"]["evil"] = "x"
    b["steps"][4]["when"] = "steps.zz.x > 1"
    assert {"template_unknown", "arg_unknown", "condition"} <= codes(lint(b, tpl))


def test_http_executor_binding_lint(act):
    tpl = act.catalogue.approved()
    base = {"key": "iis-awx", "title": "IIS via AWX", "risk": "R1", "applies_to": {"os": ["windows"]},
            "params": {"host": {"type": "host", "from": ["case.host"]}, "pool": {"type": "iis_pool", "from": ["evidence.app_pool", "ask"]}},
            "steps": [{"id": "a1", "type": "act", "executor": "http", "reversibility": "transient", "title": "AWX recycle",
                       "exec": {"awx": {"connector": "awx-prod", "job_template": 42, "vars": {"pool": "{pool}"}, "limit": "{host}"}}, "verify": "v1"},
                      {"id": "v1", "type": "verify", "checks": [{"http_ok": {"url": "{inventory.health_url}", "probe_from": "server"}, "independent": True}]}]}
    assert errors(lint(base, tpl)) == []
    bad = copy.deepcopy(base)
    bad["steps"][0]["exec"]["awx"]["vars"]["target"] = "{host}"
    bad["steps"][0]["exec"]["awx"]["job_template"] = "{pool}"
    bad["steps"][0]["exec"]["awx"]["extra"] = "x-{host}"
    assert {"exec_host", "exec_job"} <= codes(lint(bad, tpl))
    two = copy.deepcopy(base)
    two["steps"][0]["exec"] = {"awx": {"connector": "a", "job_template": 1}, "jenkins": {"connector": "j", "job": "x"}}
    assert "exec_binding" in codes(lint(two, tpl))
    nob = copy.deepcopy(base)
    del nob["steps"][0]["exec"]
    assert "template_missing" in codes(lint(nob, tpl))
    wrong = copy.deepcopy(base)
    wrong["steps"][0]["executor"] = "teleport"
    assert "executor" in codes(lint(wrong, tpl))
    h1, h2 = step_hashes(base, tpl), step_hashes(dict(base, steps=[dict(base["steps"][0], executor=""), base["steps"][1]]), tpl)
    assert h1["a1"] != h2["a1"] and h1["v1"] == h2["v1"]                  # the executor is part of the step's semantic hash


def _publish(act, key, body, author="a@corp.com", reviewer="b@corp.com", tabletop=True):
    act.runbooks.create(key, body.get("title", key), 4, body, author)
    act.runbooks.submit(key, 1, author, tabletop=tabletop)
    return act.runbooks.review(key, 1, reviewer, "approve")


def test_identity_rules_and_publication(act):
    with pytest.raises(RunbookError) as e:                                 # automatable act steps need a dry-run or the tabletop check
        act.runbooks.create("varlog", "x", 4, varlog(), "a@corp.com")
        act.runbooks.submit("varlog", 1, "a@corp.com")
    assert e.value.code == "act_dry_run_required"
    act.runbooks.submit("varlog", 1, "a@corp.com", tabletop=True)
    with pytest.raises(RunbookError) as e:
        act.runbooks.review("varlog", 1, "a@corp.com", "approve")          # author != reviewer (I6)
    assert e.value.code == "act_sod"
    from watchover.act.approvals import ApprovalError
    with pytest.raises(ApprovalError):
        act.runbooks.review("varlog", 1, "system", "approve")             # the system never counts as a person
    with pytest.raises(ApprovalError):
        act.runbooks.review("varlog", 1, "mcp:key-1", "approve")
    v = act.runbooks.review("varlog", 1, "b@corp.com", "approve")
    assert v["status"] == "approved" and act.runbooks.get("varlog")["current_version"] == 1
    ev = [x["event"] for x in act.audit.events(object_kind="runbook")]
    assert "runbook.approved" in ev and "runbook.submitted" in ev
    pay = next(x["payload"] for x in act.audit.events(object_kind="runbook") if x["event"] == "runbook.approved")
    assert pay["body_sha256"] == act.runbooks.version("varlog", 1)["body_sha256"]       # for the dispatcher's integrity check (C35)
    with pytest.raises(RunbookError) as e:
        act.runbooks.submit("varlog", 1, "a@corp.com", tabletop=True)       # immutable after submit
    assert e.value.code == "act_immutable"


def test_r3_needs_two_including_policy(act):
    b = varlog()
    b["risk"] = "R3"
    act.runbooks.create("varlog3", "x", 4, b, "a@corp.com")
    r = act.runbooks.submit("varlog3", 1, "a@corp.com", tabletop=True)
    assert r["status"] == "in_review"
    st = act.runbooks.review("varlog3", 1, "b@corp.com", "approve")["approval"]
    assert st == {"status": "pending", "votes": 1, "need": 2, "requires": ["policy"]}
    st = act.runbooks.review("varlog3", 1, "c@corp.com", "approve")["approval"]
    assert st["status"] == "pending"                                       # two votes, but no installation act.policy holder yet
    st = act.runbooks.review("varlog3", 1, "admin2@corp.com", "approve", tags=["policy"])["approval"]
    assert st["status"] == "approved" and act.runbooks.version("varlog3", 1)["status"] == "approved"


def test_reject_and_diagnose_only_self_publish(act):
    act.runbooks.create("varlog", "x", 4, varlog(), "a@corp.com")
    act.runbooks.submit("varlog", 1, "a@corp.com", tabletop=True)
    assert act.runbooks.review("varlog", 1, "b@corp.com", "reject", "eksik")["status"] == "rejected"
    diag = {"key": "disk-check", "title": "Disk", "risk": "R0", "params": {"path": {"type": "path", "roots": ["/var/log"], "from": ["default"],
                                                                               "default": "/var/log"}},
            "steps": [{"id": "d1", "type": "diagnose", "template": "linux.disk.usage", "args": {"path": "{path}"}}]}
    act.runbooks.create("disk-check", "Disk", 4, diag, "a@corp.com")
    act.runbooks.submit("disk-check", 1, "a@corp.com")
    assert act.runbooks.review("disk-check", 1, "a@corp.com", "approve")["status"] == "approved"


def test_only_changed_step_hashes_reset(act):
    _publish(act, "varlog", varlog())
    for step in ("d1", "a1"):
        ck = f"t:4/varlog/{step}/prod/abc"
        act.db._exec("INSERT INTO act_cells (cell_key, team_id, runbook_key, step_key, env, host_class, level, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (ck, 4, "varlog", step, "prod", "web", 2, "x"))
        act.db._exec("INSERT INTO act_trust_events (ts, component, cell_key, runbook_key, step_key, outcome, credit, penalty) VALUES (?,?,?,?,?,?,?,?)",
                     ("2026-10-01T00:00:00+00:00", "exec", ck, "varlog", step, "ok", 1, 0))
    b2 = varlog()
    b2["params"]["days"]["default"] = 5                                    # only a1 references days
    v2 = act.runbooks.put_draft("varlog", b2, "a@corp.com")
    assert v2["version"] == 2 and v2["status"] == "draft"
    assert act.runbooks.version("varlog", 1)["body"]["params"]["days"]["default"] == 7     # v1 untouched
    d = act.runbooks.diff("varlog", 1, 2)
    assert [s["step"] for s in d["steps"] if s["state"] == "changed"] == ["a1"]
    act.runbooks.submit("varlog", 2, "a@corp.com", tabletop=True)
    act.runbooks.review("varlog", 2, "b@corp.com", "approve")
    cells = {r["step_key"]: r for r in act.db._exec("SELECT step_key, level FROM act_cells")}
    assert cells["a1"]["level"] == 1 and cells["d1"]["level"] == 2
    ev = {r["step_key"]: r["archived"] for r in act.db._exec("SELECT step_key, archived FROM act_trust_events")}
    assert ev == {"a1": 1, "d1": 0}
    assert act.runbooks.version("varlog", 1)["status"] == "superseded" and act.runbooks.get("varlog")["current_version"] == 2


# ---------------------------------------------------------------- API: administrators are not exempt (I6)
@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        yield cl


def login(c, email, pw=PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_api_admin_cannot_publish_own(c):
    c.svc.act.set_mode("guided", by="admin@watchover.local")
    h = login(c, *ADMIN)
    r = c.post("/api/act/runbooks", json={"key": "varlog-team", "adopt": "linux-varlog-full"}, headers=h)
    assert r.status_code == 201 and r.json()["status"] == "draft"
    assert c.post("/api/act/runbooks/varlog-team/versions/1/submit", json={"tabletop": True}, headers=h).json()["status"] == "in_review"
    r = c.post("/api/act/runbooks/varlog-team/versions/1/review", json={"decision": "approve"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "act_sod"
    c.svc.users.register("admin2@corp.com", PW, name="a2", role="admin")
    h2 = login(c, "admin2@corp.com")
    r = c.post("/api/act/runbooks/varlog-team/versions/1/review", json={"decision": "approve"}, headers=h2)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    keys = [x["key"] for x in c.get("/api/act/runbooks", headers=h).json()]
    assert "varlog-team" in keys and "linux-varlog-full" in keys
    assert c.get("/api/act/runbooks/varlog-team/diff?from=1&to=1", headers=h).json()["steps"]
    assert any(t["id"] == "linux.disk.usage" for t in c.get("/api/act/templates", headers=h).json())
