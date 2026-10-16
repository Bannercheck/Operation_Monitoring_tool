"""WP7: shadow mode (docs/design/action-agent.md §8.4, §8.5; C10, C21, C26, C47, C51): sealing at ready, blind sampling,
leak-free blind cases, decisions per attempt, peek, reason codes, agreement classes, the late flag."""
import copy
import json

import pytest

from watchover.act.importer import starter_bodies
from watchover.act.policy import PolicyError
from watchover.act.shadow import agreement, sized_rate
from watchover.act.types import Trigger, canonical, sha256_hex
from watchover.db import Database

ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
NOSPACE = "ERROR billing-api write failed: No space left on device (/var/log/app/billing.log)"


@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k", "WATCHOVER_ACT_DEMO": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app()
    svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        cl.db_path = str(tmp_path / "k.db")
        svc.act.set_mode("guided", by="admin@watchover.local")
        for email in ("a@corp.com", "b@corp.com"):
            svc.users.register(email, "Str0ng!Passw0rd", name=email[0], role="viewer")
        body = copy.deepcopy(starter_bodies()["linux-varlog-full"])
        svc.act.runbooks.create("varlog-team", body["title"], None, body, "a@corp.com")
        svc.act.runbooks.submit("varlog-team", 1, "a@corp.com", tabletop=True)
        svc.act.runbooks.review("varlog-team", 1, "b@corp.com", "approve")
        yield cl


def admin(c):
    r = c.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    return {"Authorization": "Bearer " + r.json()["token"]}


def blind_rate(c, pct):
    c.svc.act.policy.set_team(1, {"act_blind_override": pct}, by="demo@x")


def case(c, host="app-lnx-07", sev="high", key=None, team=1):
    return c.svc.act.cases.route(Trigger(source="threshold", key=key or f"threshold:{host}:disk", host=host, team_id=team, severity=sev,
                                         env="prod", title="Disk dolu: /var/log", facts={"metric": "disk_pct", "value": 93, "limit": 90},
                                         evidence=[{"kind": "pasted", "host": host, "text": NOSPACE}]))["case_id"]


def test_rate_formula_values():
    pol = {"act_blind_collect": 30, "act_blind_max": 60, "act_audit_sample_min": 10, "act_audit_per_30d": 4}
    assert sized_rate(3, 2, pol) == 0.30                       # three starts a day, collecting for L3
    assert sized_rate(1, 2, pol) == 0.30                       # daily toward L3: 0.296 → 30 %
    assert round(sized_rate(1, 3, pol), 2) == 0.59             # daily toward L4: 59 %
    assert sized_rate(0.05, 3, pol) == 0.60                    # rare units: capped at 60 %
    assert round(sized_rate(1, 4, pol), 3) == 0.133            # holding L4: 4 audits / 30 starts
    assert sized_rate(10, 4, pol) == 0.10                      # never below the audit-sample minimum


def test_agreement_classes():
    p = {"choice": "rb", "params": {"path": "/var/log"}}
    assert agreement(p, "rb", {"path": "/var/log"}) == "full"
    assert agreement(p, "rb", {"path": "/var/log/app"}) == "choice"
    assert agreement(p, "NONE", {}) == "differ"
    assert agreement(None, "rb", {}) == "n/a" and agreement({**p, "valid": 0}, "rb", {}) == "n/a"


def test_sealed_before_any_inbox_and_hash_in_audit(c, monkeypatch):
    seen = []
    real = c.svc.act.shadow.seal

    def spy(cid):
        other = Database(c.db_path)                            # another connection: what any inbox would read right now
        seen.append(other._exec("SELECT id FROM actions WHERE id=?", (cid,)))
        other.close()
        return real(cid)
    monkeypatch.setattr(c.svc.act.shadow, "seal", spy)
    cid = case(c)
    assert seen == [[]]                                        # sealed inside the opening transaction: not visible anywhere yet
    fi = c.svc.kb._exec("SELECT * FROM act_frozen_inputs WHERE case_id=?", (cid,))[0]
    assert fi["attempt"] == 1 and fi["input_sha"] == sha256_hex(fi["prompt"]) and "KANIT" in fi["prompt"] and "ADAYLAR" in fi["prompt"]
    assert fi["prompt_tokens"] <= 1200
    prop = c.svc.kb._exec("SELECT * FROM act_proposals WHERE case_id=?", (cid,))[0]
    ev = [e for e in c.svc.act.audit.events(object_kind="case", object_id=str(cid)) if e["event"] == "proposal.sealed"][0]
    assert ev["payload"]["seal"] == prop["sealed_hash"] and ev["seq"] == fi["audit_seq"] == prop["audit_seq"]
    body = {"choice": prop["choice"], "runbook_key": prop["runbook_key"], "version": prop["version"], "params": json.loads(prop["params"]),
            "ambiguous": [x for x in prop["invalid_reasons"].split(",") if x]}
    assert prop["sealed_hash"] == sha256_hex(canonical({"case": cid, "attempt": 1, "rules": body, "input_sha": fi["input_sha"]}))
    assert "choice" not in ev["payload"] and "varlog" not in json.dumps(ev["payload"])     # the seal reveals nothing


def test_blind_hidden_for_everyone_then_revealed(c):
    blind_rate(c, 100)
    cid = case(c)
    h = admin(c)
    d = c.get(f"/api/act/cases/{cid}", headers=h).json()
    assert d["proposal"]["state"] == "blind" and "rules" not in d["proposal"] and d["proposal"]["sealed_hash"]
    assert all("score" not in x and "suggested" not in x for x in d["candidates"]) and d["candidates"][0]["key"] == "varlog-team"
    row = [r for r in c.get("/api/act/inbox", headers=h).json() if r["id"] == cid][0]
    assert row["blind"] and row["runbook_key"] is None
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "varlog-team", "version": 1,
                                                     "params": {"host": "app-lnx-07", "path": "/var/log", "days": 7}}, headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["decision"] == {"choice": "varlog-team", "attempt": 1, "blind": True, "peeked": False, "agree_rules": "full",
                               "agree_model": "n/a", "reason_code": ""}
    assert out["reveal"]["tr"] == "Sistem de aynısını seçmişti ✓" and out["reveal"]["sealed_at"]
    assert out["proposals"]["rules"]["choice"] == "varlog-team" and out["proposals"]["rules"]["ambiguous"] == ["path"]
    d = c.get(f"/api/act/cases/{cid}", headers=h).json()
    assert d["proposal"]["state"] == "revealed" and d["proposal"]["rules"]["choice"] == "varlog-team"
    assert c.post(f"/api/act/cases/{cid}/decide", json={"choice": "NONE"}, headers=h).status_code == 409   # one decision per attempt


def test_peek_and_reason_codes(c):
    blind_rate(c, 100)
    cid = case(c)
    h = admin(c)
    p = c.post(f"/api/act/cases/{cid}/peek", headers=h).json()
    assert p["state"] == "revealed" and p["rules"]["choice"] == "varlog-team"
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "NONE"}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "act_reason_required"      # visible now: a different choice needs a reason
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "NONE", "reason_code": "other"}, headers=h)
    assert r.status_code == 422                                                              # "other" needs a note
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "NONE", "reason_code": "not_now", "note": "logrotate 10 dk sonra"}, headers=h)
    assert r.status_code == 201 and r.json()["decision"]["peeked"] and r.json()["decision"]["agree_rules"] == "differ"
    row = c.svc.kb._exec("SELECT * FROM act_decisions WHERE case_id=?", (cid,))[0]
    assert row["blind"] == 1 and row["peeked"] == 1 and row["reason_code"] == "not_now"


def test_never_blind_p1_no_candidate_storm_child(c):
    blind_rate(c, 100)
    p1 = case(c, host="app-lnx-08", sev="critical")
    none = c.svc.act.cases.route(Trigger(source="external", key="external:cpu:1", host="app-lnx-09", team_id=1, title="CPU"))["case_id"]
    regular = case(c, host="app-lnx-10")
    rows = {r["id"]: r for r in c.svc.kb._exec("SELECT id, blind FROM actions WHERE kind='case'")}
    assert rows[p1]["blind"] == 0 and rows[none]["blind"] == 0 and rows[regular]["blind"] == 1
    c.svc.kb._exec("UPDATE actions SET parent_id=? WHERE id=?", (p1, regular))
    assert not c.svc.act.shadow.is_blind(c.svc.act.cases.get(regular), {"candidates": [1]})   # storm children
    blind_rate(c, 0)
    assert c.svc.act.shadow.is_blind(c.svc.act.cases.get(case(c, host="app-lnx-11")), {"candidates": [1]}) is False


def test_demo_override_refused_without_demo(c, monkeypatch):
    monkeypatch.delenv("WATCHOVER_ACT_DEMO")
    with pytest.raises(PolicyError):
        c.svc.act.policy.set_team(2, {"act_blind_override": 100}, by="m@x")


def test_blind_prediagnosis_and_similar_carry_no_runbook(c):
    old = case(c, host="app-lnx-07", key="threshold:old:disk")
    c.svc.kb._exec("UPDATE actions SET phase='resolved', closed_at='2026-10-01T01:00:00+00:00', runbook_key='varlog-team', outcome='ok' "
                   "WHERE id=?", (old,))
    blind_rate(c, 100)
    cid = case(c)
    with c.svc.kb.tx():
        c.svc.act.cases._add_evidence(cid, {"kind": "diag", "host": "app-lnx-07", "template_id": "linux.disk.usage", "run_id": 9, "step_id": 3,
                                            "fields": {"mount": "/var", "use_pct": 93}})
    h = admin(c)
    d = c.get(f"/api/act/cases/{cid}", headers=h).json()
    dg = [e for e in d["evidence"] if e["kind"] == "diag"][0]
    assert dg["attribution"] is None and "run_id" not in dg["ref"] and "step_id" not in dg["ref"] and dg["ref"]["template_id"] == "linux.disk.usage"
    assert d["similar"] and all(s["runbook"] is None for s in d["similar"])
    c.post(f"/api/act/cases/{cid}/decide", json={"choice": "ESCALATE", "reason_code": "other_team"}, headers=h)
    d = c.get(f"/api/act/cases/{cid}", headers=h).json()
    assert d["similar"][0]["runbook"] == "varlog-team" and d["proposal"]["state"] == "revealed"


def test_second_decision_after_reopen(c):
    blind_rate(c, 0)
    cid = case(c)
    h = admin(c)
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "varlog-team", "version": 1,
                                                     "params": {"host": "app-lnx-07", "path": "/var/log", "days": 7}}, headers=h)
    assert r.status_code == 201
    assert c.post(f"/api/act/cases/{cid}/close", json={"outcome": "ok", "resolution": "sıkıştırıldı"}, headers=h).status_code == 200
    assert c.post(f"/api/act/cases/{cid}/reopen", json={"reason": "tekrar doldu"}, headers=h).status_code == 200
    fi = c.svc.kb._exec("SELECT attempt FROM act_frozen_inputs WHERE case_id=? ORDER BY attempt", (cid,))
    assert [x["attempt"] for x in fi] == [1, 2]                                              # a new attempt sealed at ready
    r = c.post(f"/api/act/cases/{cid}/decide", json={"choice": "NONE", "reason_code": "not_now", "note": "gece rotasyonu"}, headers=h)
    assert r.status_code == 201 and r.json()["decision"]["attempt"] == 2
    assert c.post(f"/api/act/cases/{cid}/dismiss", json={"reason": "geçti"}, headers=h).status_code == 200   # already decided: no 2nd row
    dec = c.svc.kb._exec("SELECT attempt, choice FROM act_decisions WHERE case_id=? ORDER BY attempt", (cid,))
    assert [(x["attempt"], x["choice"]) for x in dec] == [(1, "varlog-team"), (2, "NONE")]
    other = case(c, host="app-lnx-12")
    c.post(f"/api/act/cases/{other}/escalate", json={"reason": "DBA"}, headers=h)
    assert c.svc.kb._exec("SELECT choice FROM act_decisions WHERE case_id=?", (other,))[0]["choice"] == "ESCALATE"


def test_late_model_answer(c):
    blind_rate(c, 0)
    cid = case(c)
    sh = c.svc.act.shadow
    fi = sh.frozen(cid, 1)
    alias = next(k for k, v in json.loads(fi["aliases"]).items() if v == "varlog-team")
    early = sh.record_proposal(cid, 1, "act-select:demo", alias, {"path": "/var/log"}, ["E1"])
    assert early["late"] is False
    other = case(c, host="app-lnx-13")
    sh.decide(other, "ayse@corp.com", "NONE", reason_code="not_now", note="x")
    fo = sh.frozen(other, 1)
    a2 = next(k for k, v in json.loads(fo["aliases"]).items() if v == "varlog-team")
    late = sh.record_proposal(other, 1, "act-select:demo", a2, {"path": "/var/log"}, ["E1"])
    assert late["late"] is True
    row = c.svc.kb._exec("SELECT late, runbook_key FROM act_proposals WHERE case_id=? AND selector='act-select:demo'", (other,))[0]
    assert row["late"] == 1 and row["runbook_key"] == "varlog-team"
    assert c.svc.kb._exec("SELECT agree_model FROM act_decisions WHERE case_id=?", (other,))[0]["agree_model"] == "differ"


@pytest.mark.skipif(not __import__("os").environ.get("WATCHOVER_TEST_DATABASE_URL"), reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_postgresql_round(tmp_path, monkeypatch):
    """Stage 2a's SQL on PostgreSQL: evidence and slots, sealing, decide, the host lock and a lease (embedded demo custody)."""
    import os
    from watchover.act import ActService, store
    from watchover.act.fake_actuator import FakeActuator, direct
    from watchover.actions import open_store
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
    act.set_mode("guided", by="a@x")
    body = copy.deepcopy(starter_bodies()["linux-varlog-full"])
    act.runbooks.create("varlog-team", body["title"], None, body, "a@corp.com")
    act.runbooks.submit("varlog-team", 1, "a@corp.com", tabletop=True)
    act.runbooks.review("varlog-team", 1, "b@corp.com", "approve")
    cid = act.cases.route(Trigger(source="threshold", key="threshold:app-lnx-07:disk", host="app-lnx-07", env="prod", title="Disk",
                                  facts={"metric": "disk_pct"}, evidence=[{"kind": "pasted", "host": "app-lnx-07", "text": NOSPACE}]))["case_id"]
    assert act.evidence.slots(cid)["log_dir"][0]["value"] == "/var/log/app"
    assert act.shadow.frozen(cid, 1)["input_sha"]
    assert act.shadow.decide(cid, "ayse@corp.com", "varlog-team", 1, {"host": "app-lnx-07", "path": "/var/log", "days": 7})["decision"]["attempt"] == 1
    db._exec("INSERT INTO act_actuators (agent_id, host, token_sha256, key_status, next_seq) VALUES (?,?,?,?,?)", (7, "app-lnx-07", "x", "none", 1))
    j = act.jobs.dispatch(host="app-lnx-07", template_id="linux.logs.compress_old", template_sha256="", params={}, mode="execute", step_id=1, run_id=1)
    with pytest.raises(Exception):
        act.jobs.dispatch(host="app-lnx-07", template_id="linux.logs.compress_old", template_sha256="", params={}, mode="execute", step_id=2, run_id=2)
    tok = act.jobs.actuators.rotate_token(7, by="a@x")["token"]
    fa = FakeActuator(tok, "app-lnx-07", direct(act.jobs), agent_id=7)
    fa.hello()
    act.jobs.actuators.confirm_key(7, fa.fingerprint, "a@x")
    fa.signed = True
    out = fa.run_once()
    assert out and out[0]["seq"] == 1 and act.jobs.get(j["job_id"])["state"] == "done"
    assert act.audit.verify()["ok"]
    for t in store.act_tables(db) + ["actions"]:                    # leave the shared test database as other tests expect it
        db._exec(f"DROP TABLE IF EXISTS {t} CASCADE")
    db._exec("DELETE FROM rbac_meta WHERE key LIKE 'act_%'")
    db.close()
