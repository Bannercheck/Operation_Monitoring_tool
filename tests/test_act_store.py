"""WP1: the act skeleton — DDL, keys without nullable columns (C9), +00:00 timestamps, act_mode off (I14), audit in one tx()."""
import io
import os

import pytest

from watchover.act import ActService, store
from watchover.act.audit import Audit, AuditError
from watchover.act.keys import KeyClient, SigningUnavailable
from watchover.act.policy import Policy, PolicyError
from watchover.act.types import Trigger, canonical, cell_key, job_hash, stored_ts, team_key, wire_ts, worst
from watchover.actions import open_store
from watchover.db import Database

PG_URL = os.environ.get("WATCHOVER_TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WATCHOVER_ACT_DEMO", raising=False)
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    if request.param == "postgresql":
        if not PG_URL:
            pytest.skip("WATCHOVER_TEST_DATABASE_URL not set")
        d = Database(PG_URL)
        for t in store.act_tables(d):
            d._exec(f"DROP TABLE IF EXISTS {t}")
        if "auth_events" in d.tables():                              # anchor rows of an earlier test's chain (WP2)
            d._exec("DELETE FROM auth_events WHERE event='act_anchor'")
    else:
        d = Database(str(tmp_path / "k.db"))
    open_store(d, str(tmp_path / "none.db"))
    yield d
    d.close()


def test_ddl_idempotent_twice(db):
    first = store.ensure(db)
    assert set(first) == set(store.TABLES)
    assert store.ensure(db) == []
    assert set(store.act_tables(db)) >= set(store.TABLES)
    assert db._exec("SELECT seq FROM act_audit_head WHERE id=1")[0]["seq"] == 0
    assert len(db._exec("SELECT * FROM act_audit_head")) == 1


def test_no_nullable_column_in_any_key(db):
    store.ensure(db)
    keys = store.key_columns(db)
    assert keys and {t for t, *_ in keys} >= {"act_evidence", "act_decisions", "act_frozen_inputs", "act_metrics_weekly", "act_cells"}
    bad = [(t, kind, c) for t, kind, c, nullable in keys if nullable]
    assert bad == []
    assert ("act_decisions", "UNIQUE", "attempt") in [(t, k, c) for t, k, c, _ in keys]       # (case_id, attempt) keys (C10)
    mw = [c for t, k, c, _ in keys if t == "act_metrics_weekly" and k == "PRIMARY KEY"]
    assert "team_key" in mw and "team_id" not in mw


def test_timestamps_end_in_utc_offset(db):
    act = ActService(db=db)
    seq = act.audit.write("policy.changed", object_kind="policy", object_id="x", actor="a@b.c", actor_kind="user", payload={"k": 1})
    row = db._exec("SELECT ts FROM act_audit WHERE seq=?", (seq,))[0]
    assert row["ts"].endswith("+00:00")
    act.policy.set("act_watch_min", 20, by="a@b.c")
    assert db._exec("SELECT updated_at FROM act_policy WHERE key='act_watch_min'")[0]["updated_at"].endswith("+00:00")
    k = act.kill.stop("team", "4", by="a@b.c", reason="test", team_id=4)
    assert db._exec("SELECT ts FROM act_kill WHERE id=?", (k["id"],))[0]["ts"].endswith("+00:00")
    assert stored_ts("2026-10-15T11:48:03Z") == "2026-10-15T11:48:03+00:00"
    assert stored_ts("2026-10-15T14:48:03+03:00") == "2026-10-15T11:48:03+00:00"
    assert wire_ts("2026-10-15T11:48:03+00:00") == "2026-10-15T11:48:03Z"
    assert Trigger(source="manual", key="manual:1", ts="2026-10-15T11:48:03Z").ts.endswith("+00:00")


def test_mode_off_every_hook_is_a_noop(db):
    act = ActService(db=db)
    assert act.mode == "off" and not act.enabled                     # I14: an upgrade executes nothing
    before = store.counts(db)
    assert act.emit(Trigger(source="threshold", key="threshold:prod:web-07:disk", host="web-07")) is False
    assert act.emit({"source": "manual", "key": "manual:x"}) is False
    assert act.from_anomaly({"key": "threshold:prod:web-07:disk", "host": "web-07", "kind": "threshold"}) is False
    assert act.drain() == 0
    assert len(act.queue) == 0 and act.stats["emitted"] == 0 and act.stats["ignored_off"] == 2
    assert store.counts(db) == before
    assert db._exec("SELECT COUNT(*) AS n FROM actions WHERE kind='case'")[0]["n"] == 0


def test_emit_never_raises_and_queue_is_bounded(db, monkeypatch):
    act = ActService(db=db)
    act.set_mode("guided", by="admin@x")
    act.cases = None
    assert act.emit({"source": "nonsense", "key": ""}) is False and act.stats["errors"] == 1
    monkeypatch.setattr("watchover.act.QUEUE_MAX", 3)
    for i in range(5):
        assert act.emit(Trigger(source="manual", key=f"manual:{i}"))
    assert [t.key for t in act.queue] == ["manual:2", "manual:3", "manual:4"] and act.stats["dropped"] == 2


def test_audit_and_state_change_in_one_tx(db):
    act = ActService(db=db)
    aid = open_store(db).create("INC-1", "x")["id"]
    with db.tx():
        db._exec("UPDATE actions SET phase=? WHERE id=?", ("ready", aid))
        act.audit.write("case.ready", object_kind="case", object_id=aid, actor="system")
    assert db._exec("SELECT phase FROM actions WHERE id=?", (aid,))[0]["phase"] == "ready"
    head = act.audit.head()["seq"]
    with pytest.raises(RuntimeError):
        with db.tx():
            db._exec("UPDATE actions SET phase=? WHERE id=?", ("running", aid))
            act.audit.write("case.running", object_kind="case", object_id=aid)
            raise RuntimeError("after the audit row")
    assert db._exec("SELECT phase FROM actions WHERE id=?", (aid,))[0]["phase"] == "ready"
    assert act.audit.head()["seq"] == head and db.count("act_audit") == head

    def broken(*a, **k):
        raise OSError("keyd down")
    act.keys.mac_audit = broken                                     # audit write fails -> the action does not happen (I9)
    with pytest.raises(AuditError):
        with db.tx():
            db._exec("UPDATE actions SET phase=? WHERE id=?", ("dismissed", aid))
            act.audit.write("case.dismissed", object_kind="case", object_id=aid)
    assert db._exec("SELECT phase FROM actions WHERE id=?", (aid,))[0]["phase"] == "ready"


def test_audit_chain_verify_and_pseudonyms(db):
    act = ActService(db=db)
    n0 = act.audit.head()["seq"]                                     # the vendor catalogue load is audited at start-up
    for i in range(5):
        act.audit.write("trust.event", object_kind="cell", object_id=i, actor="ayse@corp.example", actor_kind="user",
                        payload={"i": i, "note": "PGPASSWORD=Canary-Env-22"})
    v = act.audit.verify()
    assert v["ok"] and v["n"] == n0 + 5 and v["placeholder"] and v["mac"] == ["p0"]
    rows = act.audit.events(object_kind="cell")
    assert all(r["actor"].startswith("op_") and "ayse" not in r["actor"] for r in rows)
    assert all("Canary-Env-22" not in canonical(r["payload"]) for r in rows)               # payloads are scrubbed
    assert db._exec("SELECT email FROM act_actors")[0]["email"] == "ayse@corp.example"
    with pytest.raises(Exception):                                  # WP2: the append-only trigger stops the edit
        db._exec("UPDATE act_audit SET event='forged' WHERE seq=?", (n0 + 3,))
    store.drop_triggers(db)                                         # what an attacker with SQL access would do first
    db._exec("UPDATE act_audit SET event='forged' WHERE seq=?", (n0 + 3,))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == n0 + 3 and v["reason"] == "mac"


def test_policy_mode_rules(db):
    act = ActService(db=db)
    assert act.set_mode("guided", by="admin@x")["previous"] == "off"
    assert act.mode == "guided"
    with pytest.raises(PolicyError) as e:
        act.set_mode("approve", by="admin@x")
    assert e.value.code == "act_risk_unaccepted"
    with pytest.raises(PolicyError) as e:
        act.policy.set("act_mode", "turbo", by="admin@x")
    assert e.value.code == "act_param_invalid"
    act.policy.set("act_residual_risk_accepted", "2026-10-15T00:00:00+00:00", by="admin@x")
    with pytest.raises(PolicyError) as e:                           # no signing keys: system execution cannot be switched on
        act.set_mode("approve", by="admin@x")
    assert e.value.code == "act_signing_unavailable"
    assert act.set_mode("off", by="admin@x")["value"] == "off"
    assert db._exec("SELECT COUNT(*) AS n FROM act_audit WHERE event='policy.changed'")[0]["n"] == 3
    # team overrides only make things stricter
    act.policy.set_team(4, {"act_budgets": {"team_auto_per_h": 5}, "act_thresholds": {"exec": {"2": 0.8}}}, by="m@x")
    t = act.policy.team(4)
    assert t["act_budgets"]["team_auto_per_h"] == 5 and t["act_budgets"]["team_l2_per_h"] == 30
    assert t["act_thresholds"]["exec"]["2"] == 0.8 and t["act_thresholds"]["exec"]["3"] == 0.90
    with pytest.raises(PolicyError):
        act.policy.set_team(4, {"act_budgets": {"team_auto_per_h": 50}}, by="m@x")
    with pytest.raises(PolicyError):
        act.policy.set_team(4, {"act_mode": "auto"}, by="m@x")


def test_key_custody(db, monkeypatch, tmp_path):
    store.ensure(db)
    pol = Policy(db)
    k = KeyClient(pol)
    assert k.custody() == "none" and not k.available() and k.cap() == 1
    with pytest.raises(SigningUnavailable):
        k.sign("job", b"{}")
    assert k.mac_audit(1, "", "d").startswith("p0:")
    with pytest.raises(PolicyError):                                # embedded custody is demo-only
        pol.set("act_key_custody", "embedded", by="a@x")
    monkeypatch.setenv("WATCHOVER_ACT_DEMO", "1")
    pol.set("act_key_custody", "embedded", by="a@x")
    k2 = KeyClient(pol, key_dir=str(tmp_path / "keys"))
    assert k2.custody() == "embedded" and k2.available() and k2.cap() == 2       # key_custody cap: L2 (I17)
    env = {"job_id": "j1", "agent_id": 7, "host": "h", "seq": 1, "mode": "diag", "nonce": "n1",
           "not_before": "2026-10-15T10:00:00Z", "expires_at": "2026-10-15T10:05:00Z"}
    body = canonical(env).encode()
    sig = k2.sign("job", body)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    import base64
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(k2.public_key().split(":", 1)[1]))
    pub.verify(base64.b64decode(sig), b"wo-job-v1\n" + body)
    m = k2.mac_audit(1, "", "d")
    assert m.startswith("e1:") and k2.verify_mac(1, "", "d", m) and not k2.verify_mac(1, "", "e", m)
    assert oct(os.stat(tmp_path / "keys" / "dispatch.key").st_mode)[-3:] == "600"


def test_types_helpers():
    assert team_key(None) == "*" and team_key(4) == "4"
    ck = cell_key(4, "linux-varlog-full", "a1", "prod", "web-prod")
    assert ck.startswith("t:4/linux-varlog-full/a1/prod/") and len(ck.rsplit("/", 1)[1]) == 12
    assert canonical({"b": 1, "a": [1.5, 2.0, "ş"]}) == '{"a":["1.5",2,"ş"],"b":1}'
    assert worst(["ok", "verify_failed", "rolled_back"]) == "rolled_back"
    assert len(job_hash("h", "t", "s", {"p": 1}, "execute", "v", "a1")) == 64
    with pytest.raises(ValueError):
        Trigger(source="bogus", key="k")


def test_doctor_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WATCHOVER_ACT_DEMO", raising=False)
    monkeypatch.delenv("WATCHOVER_KEYD", raising=False)
    from watchover.act.__main__ import doctor, main
    buf = io.StringIO()
    assert doctor(out=buf) == 0
    out = buf.getvalue()
    assert "mode: off · custody: none" in out and "act_audit" in out and "all present" in out
    assert main(["audit", "verify"]) == 0
    assert main(["kill", "--global", "--reason", "tatbikat"]) == 0
    assert (tmp_path / "home" / "act.KILL").exists()
    assert main(["kill", "--global", "--resume"]) == 0 and not (tmp_path / "home" / "act.KILL").exists()
