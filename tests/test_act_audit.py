"""WP2: watchover-keyd and the keyed audit chain (docs/design/action-agent.md §9.3.1, §10.7; I9, I17, I19; R3b, R3c, C11)."""
import base64
import gzip
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest

from watchover.act import ActService, keyd as keydmod, store
from watchover.act.audit import AuditError, verify_anchor_sig
from watchover.act.keyd import KeyStore, KeydError
from watchover.act.keys import KeyClient
from watchover.act.types import canonical
from watchover.db import Database

PG_URL = os.environ.get("WATCHOVER_TEST_DATABASE_URL", "")


@pytest.fixture
def keyd(monkeypatch):
    """A keyd server on a Unix socket, its keys in a directory of its own (never under WATCHOVER_HOME)."""
    d = Path(tempfile.mkdtemp(prefix="wk"))
    ks = KeyStore(d / "keys", durable=False)
    ks.create()
    srv = keydmod.start_thread(ks, unix=str(d / "keyd.sock"))
    monkeypatch.setenv("WATCHOVER_KEYD", f"unix:{d / 'keyd.sock'}")
    monkeypatch.setenv("WATCHOVER_KEYD_TIMEOUT_MS", "2000")
    yield ks
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WATCHOVER_ACT_DEMO", raising=False)
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
    yield d
    d.close()


def _write(act, n, start=0):
    for i in range(start, start + n):
        act.audit.write("trust.event", object_kind="cell", object_id=i, actor="ayse@corp.example", actor_kind="user", payload={"i": i})


# ---------------------------------------------------------------- keyd itself (I9)
def test_keyd_macs_only_the_next_seq(tmp_path):
    ks = KeyStore(tmp_path / "k", durable=False)
    ks.create()
    m1 = ks.mac_audit(1, "", "d1")
    assert m1.startswith("k1:")
    with pytest.raises(KeydError) as e:                              # a skipped seq
        ks.mac_audit(3, m1, "d3")
    assert e.value.code == "seq" and e.value.extra["head"]["seq"] == 1
    with pytest.raises(KeydError):                                   # the right seq with a wrong prev_mac
        ks.mac_audit(2, "k1:" + "0" * 64, "d2")
    m2 = ks.mac_audit(2, m1, "d2")
    with pytest.raises(KeydError):                                   # an old seq is never MAC'd again
        ks.mac_audit(1, "", "forged")
    m2b = ks.mac_audit(2, m1, "d2-retry")                            # the unconfirmed newest one may be reissued (rolled-back tx) ...
    assert m2b != m2
    ks.mac_audit(3, m2b, "d3")                                       # ... until the next row confirms it
    with pytest.raises(KeydError):
        ks.mac_audit(2, m1, "again")
    assert ks.verify([[1, "", "d1", m1]])["bad"] == -1 and ks.verify([[1, "", "dX", m1]])["bad"] == 0
    log = [json.loads(x)["op"] for x in (tmp_path / "k" / "sig.log").read_text().splitlines()]
    assert "reissue" in log and log.count("refuse_mac") == 4


def test_keyd_anchor_signs_its_own_head(tmp_path):
    ks = KeyStore(tmp_path / "k", durable=False)
    ks.create()
    m1 = ks.mac_audit(1, "", "d1")
    m2 = ks.mac_audit(2, m1, "d2")
    a = ks.sign_anchor({"seq": 999, "mac": "k1:forged"})              # whatever the API sends ...
    assert (a["seq"], a["mac"]) == (1, m1)                           # ... keyd signs its own confirmed head
    a = ks.sign_anchor({"seq": 2, "mac": m2})                        # the committed head confirms keyd's newest MAC
    assert (a["seq"], a["mac"]) == (2, m2)
    assert verify_anchor_sig(a, ks.public()["anchor"]) and not verify_anchor_sig({**a, "seq": 3}, ks.public()["anchor"])
    assert [x["seq"] for x in ks.anchors()] == [1, 2]


def test_keyd_job_rules(tmp_path):
    ks = KeyStore(tmp_path / "k", durable=False)
    ks.create()
    (tmp_path / "k" / "keyd.json").write_text(json.dumps({"act_per_hour": 120, "act_per_host_hour": 2, "max_window_s": 600}))
    env = {"job_id": "j1", "agent_id": 7, "host": "web-01", "seq": 1, "mode": "act", "nonce": "n1",
           "not_before": "2026-10-15T10:00:00Z", "expires_at": "2026-10-15T10:05:00Z"}
    s = ks.sign_job(env)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(ks.public()["dispatch"].split(":", 1)[1])).verify(
        base64.b64decode(s["sig"]), b"wo-job-v1\n" + keydmod._canon(env))
    with pytest.raises(KeydError) as e:
        ks.sign_job({**env, "job_id": "j2"})                          # seq not above the last signed for this agent
    assert e.value.code == "replay"
    with pytest.raises(KeydError) as e:
        ks.sign_job({**env, "seq": 2, "expires_at": "2026-10-15T10:11:00Z"})
    assert e.value.code == "window"
    with pytest.raises(KeydError) as e:
        ks.sign_job({k: v for k, v in env.items() if k != "nonce"} | {"seq": 2})
    assert e.value.code == "envelope"
    ks.sign_job({**env, "seq": 2})
    with pytest.raises(KeydError) as e:                              # keyd.json: two act jobs an hour per host
        ks.sign_job({**env, "seq": 3})
    assert e.value.code == "limit"
    ks.sign_job({**env, "seq": 3, "mode": "diag"})                    # diagnose jobs are not act jobs
    (tmp_path / "k" / "KILL").write_text("{}")
    with pytest.raises(KeydError) as e:
        ks.sign_job({**env, "seq": 9, "host": "other"})
    assert e.value.code == "killed"
    assert KeyStore(tmp_path / "k")._job_state()["7"] == 3            # the last signed seq survives a restart
    with pytest.raises(KeydError):
        ks.sign("job", b"{}")                                        # job signatures only through sign_job


def test_keyd_export_needs_two_halves(tmp_path):
    ks = KeyStore(tmp_path / "k", durable=False)
    ks.create()
    t = ks.export_begin("first-half-1", by="a@x")["token"]
    with pytest.raises(KeydError):
        ks.export_finish(t, "second-half", by="a@x")                 # the same person twice
    t = ks.export_begin("first-half-1", by="a@x")["token"]
    blob = ks.export_finish(t, "second-half", by="b@x")["blob"]
    files = keydmod.open_export(blob, "first-half-1", "second-half")
    assert files["audit.key"] == (tmp_path / "k" / "audit.key").read_text().strip()
    with pytest.raises(KeydError):
        keydmod.open_export(blob, "first-half-1", "wrong-half!")


def test_keyd_mutual_tls(tmp_path, monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="wt"))
    keydmod.tls_init(str(d / "tls"), str(d / "client"))
    ks = KeyStore(d / "keys", durable=False)
    ks.create()
    srv = keydmod.start_thread(ks, tcp="127.0.0.1:0", tls=str(d / "tls"))
    port = srv.server_address[1]
    try:
        monkeypatch.setenv("WATCHOVER_KEYD_TLS", str(d / "client"))
        monkeypatch.setenv("WATCHOVER_KEYD_NAME", "localhost")
        k = KeyClient(None, endpoint=f"tcp://127.0.0.1:{port}")
        assert k.custody() == "keyd" and k.available() and k.mac_audit(1, "", "d").startswith("k1:")
        monkeypatch.setenv("WATCHOVER_KEYD_TLS", "")                  # no client certificate: refused
        k2 = KeyClient(None, endpoint=f"tcp://127.0.0.1:{port}")
        assert k2.keyd_status() is None and not k2.available()
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- the chain through keyd
def test_thousand_events_verify_and_tamper_is_found(db, keyd):
    act = ActService(db=db)
    assert act.keys.custody() == "keyd"
    n0 = act.audit.head()["seq"]
    _write(act, 1000)
    v = act.audit.verify()
    assert v["ok"] and v["n"] == n0 + 1000 and "k1" in v["mac"], v
    assert keyd.head()["seq"] == n0 + 1000
    # the triggers stop UPDATE and DELETE (I9: append-only)
    for sql in ("UPDATE act_audit SET event='x' WHERE seq=5", "DELETE FROM act_audit WHERE seq=5",
                "UPDATE act_audit_payload SET payload='{}' WHERE seq=5", "DELETE FROM act_audit_payload WHERE seq=5"):
        with pytest.raises(Exception):
            db._exec(sql)
    assert db.count("act_audit") == n0 + 1000
    store.drop_triggers(db)
    s_edit, s_del = n0 + 400, n0 + 700
    db._exec("UPDATE act_audit SET event='forged' WHERE seq=?", (s_edit,))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == s_edit and v["reason"] == "mac"
    row = db._exec("SELECT * FROM act_audit WHERE seq=?", (s_edit,))[0]
    db._exec("UPDATE act_audit SET event='trust.event' WHERE seq=?", (s_edit,))
    assert act.audit.verify()["ok"]
    a, b = (db._exec("SELECT * FROM act_audit WHERE seq=?", (s,))[0] for s in (s_del, s_del + 1))   # reorder two rows
    db._exec("UPDATE act_audit SET ts=?, object_id=? WHERE seq=?", (b["ts"], b["object_id"], s_del))
    db._exec("UPDATE act_audit SET ts=?, object_id=? WHERE seq=?", (a["ts"], a["object_id"], s_del + 1))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == s_del
    db._exec("UPDATE act_audit SET ts=?, object_id=? WHERE seq=?", (a["ts"], a["object_id"], s_del))
    db._exec("UPDATE act_audit SET ts=?, object_id=? WHERE seq=?", (b["ts"], b["object_id"], s_del + 1))
    db._exec("DELETE FROM act_audit WHERE seq=?", (s_del,))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == s_del and v["reason"] == "gap"
    assert row["seq"] == s_edit


def test_rolled_back_write_leaves_no_gap(db, keyd):
    act = ActService(db=db)
    _write(act, 3)
    with pytest.raises(RuntimeError):
        with db.tx():
            act.audit.write("case.ready", object_kind="case", object_id=1)
            raise RuntimeError("after the audit row")                   # keyd MAC'd a row that never committed
    _write(act, 2)                                                     # the next write reissues that seq
    assert act.audit.verify()["ok"]


def test_keyd_down_is_503_and_nothing_happens(db, keyd, monkeypatch):
    act = ActService(db=db)
    _write(act, 1)
    monkeypatch.setenv("WATCHOVER_KEYD", "unix:/nonexistent/keyd.sock")
    act.keys._st = (0.0, None)
    from watchover.actions import open_store
    aid = open_store(db).create("INC-1", "x")["id"]
    with pytest.raises(AuditError) as e:
        with db.tx():
            db._exec("UPDATE actions SET phase=? WHERE id=?", ("dismissed", aid))
            act.audit.write("case.dismissed", object_kind="case", object_id=aid)
    assert e.value.detail()["code"] == "act_signing_unavailable"
    assert db._exec("SELECT phase FROM actions WHERE id=?", (aid,))[0]["phase"] != "dismissed"
    assert act.keys.cap() == 1 and not act.keys.available()


def test_eight_threads_stay_linear(db, keyd):
    act = ActService(db=db)
    n0 = act.audit.head()["seq"]
    errs = []

    def run(k):
        try:
            for i in range(25):
                act.audit.write("trust.event", object_kind="cell", object_id=f"{k}-{i}")
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=run, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    seqs = [r["seq"] for r in db._exec("SELECT seq FROM act_audit ORDER BY seq")]
    assert seqs == list(range(1, n0 + 201)) and act.audit.verify()["ok"]


def test_purge_and_erasure_keep_verification(db, keyd):
    act = ActService(db=db)
    _write(act, 10)
    top = act.audit.head()["seq"]
    with pytest.raises(Exception):                                   # above the horizon a payload cannot be deleted
        db._exec("DELETE FROM act_audit_payload WHERE seq=?", (top,))
    r = act.audit.purge_payloads(upto_seq=top - 3)
    assert r["deleted"] == top - 3 and db.count("act_audit_payload") == 4   # three kept + the audit.purged row
    v = act.audit.verify()
    assert v["ok"] and v["purged"] == top - 3
    assert act.audit.erase_actor("ayse@corp.example", by="dpo@corp.example") == 1
    assert db._exec("SELECT email, erased_at FROM act_actors WHERE email=''") and act.audit.verify()["ok"]


def test_restore_is_append_only_and_lost_rows_show_as_a_gap(db, keyd):
    act = ActService(db=db)
    _write(act, 5)
    old = db.dump()
    _write(act, 5, start=5)
    n = act.audit.head()["seq"]
    db.restore(old)                                                   # an older snapshot: deletes nothing (C11)
    assert db.count("act_audit") == n and act.audit.head()["seq"] == n and act.audit.verify()["ok"]
    # rows lost anyway (an external restore, or deleted after the triggers were dropped): keyd's head stays ahead
    store.drop_triggers(db)
    lost_from = n - 2
    db._exec("DELETE FROM act_audit WHERE seq>=?", (lost_from,))
    db._exec("DELETE FROM act_audit_payload WHERE seq>=?", (lost_from,))
    last = db._exec("SELECT seq, mac FROM act_audit ORDER BY seq DESC LIMIT 1")[0]
    db._exec("UPDATE act_audit_head SET seq=?, mac=? WHERE id=1", (last["seq"], last["mac"]))
    store.ensure_triggers(db)
    seq = act.audit.write("case.ready", object_kind="case", object_id=1)   # writing continues after keyd's head
    assert seq == n + 2
    gap = db._exec("SELECT event FROM act_audit WHERE seq=?", (n + 1,))[0]
    assert gap["event"] == "chain.gap"
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == lost_from and v["reason"] == "gap"
    assert act.audit.chain_cap() == 2                                 # chain cap until two act.policy holders acknowledge
    db._exec("INSERT INTO act_changes (kind, body, requested_by, requested_at, status) VALUES ('chain_ack', ?, 'a@x', '', 'applied')",
             (json.dumps({"broken_at": lost_from}),))
    v = act.audit.verify()
    assert v["ok"] and v["acknowledged"] == [[lost_from, n]] and act.audit.chain_cap() == 4


def test_keyd_adopts_the_placeholder_head_once(db, monkeypatch, keyd):
    ep = os.environ["WATCHOVER_KEYD"]
    monkeypatch.delenv("WATCHOVER_KEYD")
    act = ActService(db=db)
    _write(act, 3)                                                   # no custody: p0 rows
    assert act.audit.verify()["mac"] == ["p0"]
    shutil.rmtree(keyd.dir)                                           # a fresh keyd whose keys are created when act_mode leaves off
    keyd._keys.clear()
    keyd._head = None
    monkeypatch.setenv("WATCHOVER_KEYD", ep)
    act.keys._st = (0.0, None)
    act.keys.policy = act.policy
    assert act.keys.custody() == "keyd" and not act.keys.has_keys()
    _write(act, 1, start=3)                                           # keyd without keys: still p0
    act.set_mode("guided", by="admin@x")                              # creates the keys (§15.3 step 6)
    assert act.keys.has_keys()
    _write(act, 2, start=4)
    v = act.audit.verify()
    assert v["ok"] and v["mac"] == ["k1", "p0"]
    # a p0 row forged after keyd took over is a break, not a "placeholder"
    store.drop_triggers(db)
    h = act.audit.head()
    from watchover.act.keys import p0 as p0mac
    from watchover.act.audit import _digest
    row = {"seq": h["seq"] + 1, "ts": "2026-10-16T00:00:00+00:00", "actor": "x", "actor_kind": "system", "event": "forged",
           "object_kind": "", "object_id": "", "team_id": None, "payload_sha256": "0" * 64}
    mac = p0mac(h["mac"], _digest(row))
    db._exec("INSERT INTO act_audit (seq, ts, actor, actor_kind, event, object_kind, object_id, team_id, payload_sha256, prev_mac, mac) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (row["seq"], row["ts"], "x", "system", "forged", "", "", None, "0" * 64, h["mac"], mac))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == h["seq"] + 1


# ---------------------------------------------------------------- anchors (I19 helper) and sinks
class _Stub(http.server.BaseHTTPRequestHandler):
    got = []

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Stub.got.append((self.path, dict(self.headers), self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_anchor_freshness_against_sink_stubs(db, keyd, tmp_path):
    act = ActService(db=db)
    _write(act, 3)
    an = act.anchors
    assert an.freshness() == {"configured": False, "fresh": False, "age_min": None, "last_ts": None, "max_age_min": 120.0}
    assert an.cap() == 2                                              # no sink: every act step ≤ L2 (I19)
    worm = tmp_path / "worm"
    worm.mkdir()
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        an.set_sinks([{"kind": "worm_share", "target": str(worm)},
                      {"kind": "s3_object_lock", "target": f"http://127.0.0.1:{srv.server_address[1]}/bucket"}], by="a@x")
        a = an.anchor("test")
        h = act.audit.head()
        assert a["seq"] == h["seq"] - 1                               # the anchor row itself is audited after the anchor
        assert all(x["ok"] for x in a["acks"].values())
        files = list(worm.iterdir())
        assert len(files) == 1 and json.loads(files[0].read_text())["mac"] == a["mac"]
        path, headers, body = _Stub.got[-1]
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers.get("x-amz-object-lock-mode") == "COMPLIANCE" and json.loads(body)["seq"] == a["seq"]
        fr = an.freshness()
        assert fr["fresh"] and an.cap() == 4
        # the newest acknowledged anchor gets old → stale → L2
        db._exec("INSERT INTO act_anchors (seq, mac, ts, sig) VALUES (?,?,?,?)", (1, "x", "2026-01-01T00:00:00+00:00", ""))
        store.drop_triggers(db)
        db._exec("UPDATE act_anchors SET ts='2026-01-01T00:00:00+00:00'")
        store.ensure_triggers(db)
        assert not an.freshness()["fresh"] and an.cap() == 2
        ev = db._exec("SELECT detail FROM auth_events WHERE event='act_anchor'") if "auth_events" in db.tables() else []
        assert all("seq=" in e["detail"] for e in ev)
    finally:
        srv.shutdown()


def test_anchor_mismatch_is_a_break(db, keyd):
    act = ActService(db=db)
    _write(act, 4)
    a = act.anchors.anchor("test")
    assert act.audit.verify()["ok"] and act.audit.last["anchors"]["n"] >= 1
    store.drop_triggers(db)
    db._exec("UPDATE act_audit SET mac=? WHERE seq=?", ("k1:" + "f" * 64, a["seq"]))
    v = act.audit.verify()
    assert not v["ok"] and v["broken_at"] == a["seq"]


def test_anchor_on_kill_and_policy_events(db, keyd):
    act = ActService(db=db)
    act.set_mode("guided", by="admin@x")
    assert act.anchors._pending                                       # policy.changed asks for an anchor
    assert act.anchors.tick() is not None and not act.anchors._pending
    act.kill.stop("global", "", by="ops@x", reason="tatbikat")
    assert act.anchors._pending


# ---------------------------------------------------------------- I17: no key under /data, none in a snapshot archive
def test_no_key_under_data_or_in_snapshots(db, keyd, tmp_path, monkeypatch):
    from watchover import admin, vault
    home = Path(os.environ["WATCHOVER_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / ".vault.key").write_bytes(b"legacy")
    monkeypatch.setenv("WATCHOVER_VAULT_DIR", str(tmp_path / "secrets"))
    assert vault.key_path() == tmp_path / "secrets" / ".vault.key" and not (home / ".vault.key").exists()
    assert (tmp_path / "secrets" / ".vault.key").read_bytes() == b"legacy"     # moved once, not regenerated
    act = ActService(db=db)
    act.set_mode("guided", by="admin@x")
    _write(act, 3)
    act.anchors.anchor("test")
    found = [p for p in home.rglob("*") if p.is_file() and (p.suffix == ".key" or p.name in ("head.json", "sig.log"))]
    assert found == []
    root = tmp_path / "repo"
    (root / "act_keys").mkdir(parents=True)
    (root / "act_keys" / "audit.key").write_text("x")
    (root / "x.key").write_text("x")
    (root / "code.py").write_text("print(1)")
    monkeypatch.setenv("WATCHOVER_VERSIONS", str(tmp_path / "versions"))
    meta = admin.snapshot("test", db=False, root=root)
    with zipfile.ZipFile(admin.versions_dir() / meta["id"] / "code.zip") as zf:
        names = zf.namelist()
    assert "watchover/code.py" in names and not any(n.endswith(".key") for n in names)


def test_doctor_warns_on_a_shared_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("WATCHOVER_ACT_DEMO", "1")
    k = KeyClient(None, key_dir=str(tmp_path / "home" / "act_keys"))
    k.wanted = lambda: "embedded"                                    # noqa: E731
    assert any("inside WATCHOVER_HOME" in w for w in k.shared_mount())
    st = {"dir": str(tmp_path / "keys"), "st_dev": os.stat(tmp_path / "home").st_dev}
    assert any("shares a file system" in w for w in k.shared_mount(st))
    assert k.shared_mount({"dir": "/elsewhere", "st_dev": -1}) == []


def test_doctor_and_cli(tmp_path, monkeypatch, keyd):
    from watchover.act import __main__ as cli
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    d = Database(str(tmp_path / "k.db"))
    act = ActService(db=d)
    _write(act, 3)
    out = io.StringIO()
    assert cli.doctor(out=out) == 0
    text = out.getvalue()
    assert "append-only triggers: present" in text and "external anchor: no sink configured" in text and "custody: keyd" in text
    assert cli.main(["audit", "verify"]) == 0
    store.drop_triggers(d)
    d._exec("UPDATE act_audit SET event='x' WHERE seq=2")
    assert cli.main(["audit", "verify"]) == 1


# ---------------------------------------------------------------- migration: triggers after the copy (C11)
@pytest.mark.skipif(not PG_URL, reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_migrate_creates_triggers_after_the_copy(tmp_path, monkeypatch, keyd):
    from watchover import migrate
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    src = Database(str(tmp_path / "knowledge.db"))
    act = ActService(db=src)
    _write(act, 5)
    n = src.count("act_audit")
    src.close()
    pg = Database(PG_URL)
    for t in store.act_tables(pg):
        pg._exec(f"DROP TABLE IF EXISTS {t}")
    pg.close()
    rep = migrate.migrate(source=str(tmp_path), target=PG_URL)
    assert rep["tables"]["act_audit"] == n and rep["triggers"] > 0
    pg = Database(PG_URL)
    assert store.triggers_present(pg) and pg._exec("SELECT seq FROM act_audit_head")[0]["seq"] == n
    with pytest.raises(Exception):
        pg._exec("DELETE FROM act_audit")
    assert migrate.migrate(source=str(tmp_path), target=PG_URL)["tables"]["act_audit"] == 0   # re-run copies nothing twice
    pg.close()


# ---------------------------------------------------------------- API: two act.policy holders, 503 when keyd is down
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
    svc.users.register("sec@corp.com", "Str0ng!Passw0rd", name="sec", role="admin")
    with TestClient(a) as cl:
        cl.svc = svc
        yield cl


def _login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    h = {"Authorization": "Bearer " + r.json()["token"]}
    assert c.post("/api/auth/stepup/verify", json={"password": pw}, headers=h).status_code == 200
    return h


def test_api_two_holders_for_sinks_and_ack(c, tmp_path, monkeypatch):
    c.svc.act.set_mode("guided", by="admin@watchover.local")
    a = _login(c, "admin@watchover.local", "Watchover!Admin2026")
    b = _login(c, "sec@corp.com", "Str0ng!Passw0rd")
    worm = tmp_path / "worm"
    worm.mkdir()
    r = c.put("/api/act/anchors/sinks", json={"sinks": [{"kind": "worm_share", "target": str(worm)}]}, headers=a)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    cid = r.json()["id"]
    assert c.get("/api/act/anchors/sinks", headers=a).json() == []
    r = c.post(f"/api/act/changes/{cid}/confirm", json={}, headers=a)  # the requester cannot confirm
    assert r.status_code == 403
    r = c.post(f"/api/act/changes/{cid}/confirm", json={}, headers=b)
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    assert c.get("/api/act/anchors/sinks", headers=a).json()[0]["kind"] == "worm_share"
    r = c.post("/api/act/anchors/now", headers=a)
    assert r.status_code == 201 and list(worm.iterdir())
    st = c.get("/api/act/status", headers=a).json()
    assert st["chain"]["anchor_fresh"] and st["caps"]["anchor"] == 4 and st["custody"] == "keyd"
    v = c.get("/api/act/audit/verify", headers=a).json()
    assert v["ok"] and v["anchors"]["n"] >= 1, v
    r = c.get("/api/act/audit/export", headers=a)
    assert r.status_code == 200 and r.text.count("\n") >= v["n"] - 1
    r = c.post("/api/act/audit/acknowledge", json={"broken_at": 7, "reason": "drill"}, headers=a)
    assert r.status_code == 202
    r = c.post(f"/api/act/changes/{r.json()['id']}/confirm", json={}, headers=b)
    assert r.json()["status"] == "applied" and c.svc.act.audit.acknowledged(7)
    # export: one pass-phrase half each
    r = c.post("/api/act/keys/export", json={"part": "first-half-A"}, headers=a)
    assert r.status_code == 202
    r = c.post(f"/api/act/changes/{r.json()['id']}/confirm", json={"inputs": {"part": "second-half-B"}}, headers=b)
    blob = r.json()["result"]["blob"]
    assert "audit.key" in keydmod.open_export(blob, "first-half-A", "second-half-B")
    # keyd down: a state change that needs an audit row answers 503 and does not happen
    monkeypatch.setenv("WATCHOVER_KEYD", "unix:/nonexistent/keyd.sock")
    c.svc.act.keys._st = (0.0, None)
    r = c.post("/api/act/kill", json={"scope": "global", "reason": "x"}, headers=a)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "act_signing_unavailable"
    assert c.svc.kb.count("act_kill") == 0
