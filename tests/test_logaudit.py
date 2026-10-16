"""The audit-safe archive (logaudit.py, logtiers.py): the hash-chained manifest and its verification (a changed, deleted or
reordered entry, an altered or deleted archive, a cut tail), sealed archives, first-run registration, retention as the only thing
that deletes an archive (a size cap alerts instead), the full archive disk (archiving pauses, the day stays hot, no loss), moving a
tier to a new directory without loss (and refusing without room or into a foreign directory), auditor exports with their own
checksum, the storage alerts through the notification engine, and the HTTP permissions."""
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from watchover import logaudit as la
from watchover import logstore
from watchover import logtiers as lt
from watchover.live import LiveStore

UTC = timezone.utc
TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
NOON = TODAY + timedelta(hours=12)
ROOMY = {"total": 4 << 40, "free": 3 << 40, "used": 1 << 40}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "audit-test")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)
    logstore._SCRUB.clear()
    monkeypatch.setattr(lt, "_disk", lambda p: dict(ROOMY))          # this machine's disk never decides an assertion


def payload(t0, host, n, step_s=60):
    recs = [{"ts": (t0 + timedelta(seconds=i * step_s)).isoformat(), "level": "error" if i % 4 == 0 else "info", "service": "ea-portal",
             "host": host, "env": "prod", "msg": f"order {100000 + i} done in {i * 3} ms", "status": 500 if i % 4 == 0 else 200} for i in range(n)]
    return ("\n".join(json.dumps(x) for x in recs) + "\n").encode()


def build(tmp_path, days=(5, 4, 3, 2), per=20, cfg=None):
    hot, cold = lt.HotStore(tmp_path / "hot"), lt.ColdStore(tmp_path / "cold")
    st = LiveStore(maxlen=5)
    st.retention_h = 24 * 30
    st.attach_db(hot)
    st.cold = cold
    for d in days:
        for h in ("ea-app-01", "hr-app-01"):
            st.ingest("app.jsonl", payload(TODAY - timedelta(days=d) + timedelta(hours=9), h, per), agent=h)
    st.ingest("app.jsonl", payload(datetime.now(UTC) - timedelta(minutes=20), "ea-app-01", 3, step_s=1), agent="ea-app-01")
    c = {"live_retention_h": 48, "cold_retention_days": 365, **(cfg or {})}
    return st, hot, cold, lt.LogTiers(hot, cold, lambda: c, subprocess_ok=False)


def stored_lines(hot, cold) -> set:
    out = set()
    for r in hot._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches"):
        out |= {ln for ln in logstore.row_bytes(r).split(b"\n") if ln}
    for d in cold.days():
        for r in cold.iter_day(d):
            out |= {ln for ln in r["_text"].split(b"\n") if ln}
    return out


def codes(res) -> set:
    return {p["code"] for p in res["problems"]}


# ---------------------------------------------------------------- manifest, chain, verify
def test_archives_are_sealed_and_chained_and_verify_finds_every_tampering(tmp_path):
    st, hot, cold, tiers = build(tmp_path)
    out = tiers.tick(now=NOON)
    days = cold.days()
    assert len(days) == 3 and not out["errors"] and all(a["sha256"] and a["seq"] for a in out["archived"])
    for d in days:
        assert stat.S_IMODE(cold.path(d).stat().st_mode) == 0o440                     # sealed: read-only
    ents = cold.manifest.entries()
    assert [e["op"] for e in ents] == ["seal"] * 3 and [e["seq"] for e in ents] == [1, 2, 3] and ents[0]["prev"] == la.ZERO
    assert all(ents[i]["prev"] == ents[i - 1]["hash"] and ents[i]["hash"] == la.entry_hash(ents[i]) for i in range(1, 3))
    e0 = ents[0]
    assert e0["sha256"] == hashlib.sha256(cold.path(e0["day"]).read_bytes()).hexdigest() and e0["lines"] == 40 and e0["first"] <= e0["last"]
    assert cold.manifest.head() == {"seq": 3, "hash": ents[2]["hash"], "at": ents[2]["at"]} and tiers.anchor() == 3
    ok = la.verify(cold.dir)
    assert ok["ok"] and ok["checked"] == 3 and not ok["problems"] and tiers.check["ok"]
    mf = cold.manifest.path
    good = mf.read_bytes()

    # an archive altered in place (same size): only the sha256 shows it
    p = cold.path(days[1])
    os.chmod(p, 0o640)
    b = bytearray(p.read_bytes()); b[len(b) // 2] ^= 0xFF; p.write_bytes(bytes(b)); os.chmod(p, 0o440)
    r = la.verify(cold.dir)
    assert not r["ok"] and {x["day"] for x in r["problems"] if x["code"] == "altered"} == {days[1]}
    assert la.verify(cold.dir, deep=False)["ok"]                                       # the hourly check compares sizes only
    assert la.verify(cold.dir, frm=days[2], to=days[2])["ok"]                          # a range outside the altered day
    # an archive deleted
    os.chmod(p, 0o640); p.unlink()
    r = la.verify(cold.dir, deep=False)
    assert "missing" in codes(r) and any(x["day"] == days[1] for x in r["problems"])
    # a manifest entry changed (its hash kept), deleted, reordered, the tail cut
    lines = good.splitlines(keepends=True)
    e = json.loads(lines[0]); e["sha256"] = "0" * 64
    mf.write_bytes(json.dumps(e, sort_keys=True, separators=(",", ":")).encode() + b"\n" + b"".join(lines[1:]))
    assert "entry_altered" in codes(la.verify(cold.dir, deep=False))
    mf.write_bytes(lines[0] + lines[2])
    assert "chain_broken" in codes(la.verify(cold.dir, deep=False))
    mf.write_bytes(lines[0] + lines[2] + lines[1])
    assert {"chain_broken", "seq_order"} <= codes(la.verify(cold.dir, deep=False))
    mf.write_bytes(lines[0] + lines[1])
    assert "truncated" in codes(la.verify(cold.dir, deep=False))                       # MANIFEST.head says 3
    (cold.dir / la.HEAD).unlink()
    assert "truncated" in codes(la.verify(cold.dir, deep=False, anchor_seq=tiers.anchor()))   # the hot disk remembers 3 too
    # the hourly check raises the integrity alert
    mf.write_bytes(good)
    tiers.tick(now=NOON + timedelta(minutes=1))
    rep = tiers.report()
    assert not tiers.check["ok"] and any(a["code"] == "archive_integrity" and a["level"] == "crit" for a in rep["alerts"])


def test_first_run_registers_old_archives_then_never_silently(tmp_path):
    st, hot, cold, tiers = build(tmp_path, days=(4, 3))
    for d in [x for x in hot.days() if x < lt.day_key(NOON - timedelta(days=2))]:   # archived by an older version: no manifest
        lt.archive_file(hot.files[d], cold.dir, d)
        hot.drop_day(d)
    os.remove(cold.manifest.path) if cold.manifest.exists() else None
    assert len(cold.days()) == 2 and not cold.manifest.exists()
    out = tiers.tick(now=NOON)
    assert sorted(out["registered"]) == cold.days()
    ents = cold.manifest.entries()
    assert {e["op"] for e in ents} == {"register"} and all("registered later" in e["note"] for e in ents) and la.verify(cold.dir)["ok"]
    fday = lt.day_key(TODAY - timedelta(days=20))
    foreign = cold.dir / f"logs-{fday}.wcl"                                            # an archive nobody sealed shows up later
    foreign.write_bytes(cold.path(cold.days()[0]).read_bytes())
    out = tiers.tick(now=NOON + timedelta(minutes=5))
    assert "registered" not in out and "unregistered" in codes(tiers.check) and foreign.exists()
    assert not any(e["day"] == fday for e in cold.manifest.entries())


def test_retention_alone_deletes_and_says_so_in_the_manifest(tmp_path):
    st, hot, cold, tiers = build(tmp_path, cfg={"cold_retention_days": 3})
    tiers.tick(now=NOON)
    out = tiers.tick(now=NOON + timedelta(days=2))
    keep_from = NOON + timedelta(days=2) - timedelta(days=3)
    assert out["deleted_cold"] and all(lt.day_end(d) <= keep_from for d in out["deleted_cold"]) and all(lt.day_end(d) > keep_from for d in cold.days())
    exp = [e for e in cold.manifest.entries() if e["op"] == "expire"]
    assert [e["day"] for e in exp][-len(out["deleted_cold"]):] == out["deleted_cold"] and exp[0]["note"] == "retention 3 days" and exp[0]["sha256"]
    assert la.verify(cold.dir)["ok"]                                                   # gone by policy: not "missing"


def test_a_size_cap_never_deletes_a_young_archive_it_alerts(tmp_path):
    st, hot, cold, tiers = build(tmp_path, cfg={"cold_max_gb": 1e-9, "hot_max_gb": 0})
    n = len(stored_lines(hot, cold))
    out = tiers.tick(now=NOON)
    out2 = tiers.tick(now=NOON + timedelta(hours=1))
    assert len(cold.days()) == 3 and not out["deleted_cold"] and not out2["deleted_cold"]
    assert len(stored_lines(hot, cold)) == n
    rep = tiers.report()
    assert rep["cold"]["pct"] > 100 and any(a["code"] == "cold_90" and a["level"] == "crit" for a in rep["alerts"])
    al = tiers.alerts()
    assert any("90" in a["title"] or "%" in a["title"] for a in al) and all(a["body"] for a in al)


def test_full_archive_disk_pauses_archiving_and_keeps_the_days_hot(tmp_path, monkeypatch):
    st, hot, cold, tiers = build(tmp_path)
    before = stored_lines(hot, cold)
    hot_days = hot.days()
    real = lt.shutil.disk_usage
    tight = type("DU", (), {"total": 100 << 30, "used": 99 << 30, "free": 1 << 30})
    monkeypatch.setattr(lt.shutil, "disk_usage", lambda p: tight if str(p).startswith(str(cold.dir)) else real(p))
    out = tiers.tick(now=NOON)
    assert not out["archived"] and out["paused"]["day"] == hot_days[0] and not cold.days() and hot.days() == hot_days
    assert stored_lines(hot, cold) == before                                           # nothing deleted, nothing lost
    monkeypatch.setattr(lt, "_disk", lambda p: {"total": 100 << 30, "used": 99 << 30, "free": 1 << 30})
    rep = tiers.report()
    got = {a["code"]: a for a in rep["alerts"]}
    assert got["cold_full"]["level"] == "crit" and got["cold_90"]["pct"] == 99.0 and rep["cold"]["paused"]["day"] == hot_days[0]
    t, body = la.alert_text(got["cold_full"], rep)
    assert "paused" in t and "stays on the hot disk" in body
    monkeypatch.setattr(lt.shutil, "disk_usage", real)                                 # room again: it resumes by itself
    out = tiers.tick(now=NOON + timedelta(hours=1))
    assert len(out["archived"]) == 3 and tiers.paused is None and stored_lines(hot, cold) == before


def test_full_archive_disk_alert_reaches_the_admins_by_the_alert_engine(tmp_path):
    from watchover.db import Database
    from watchover.notify import AlertEngine, Notifier
    kb = Database(str(tmp_path / "k.db"))
    sent = []
    n = Notifier(kb, lambda: {"email": {"host": "smtp.test"}})
    import watchover.notify as wn
    orig = wn.send_email
    wn.send_email = lambda cfg, to, subj, text, html=None: sent.append((tuple(to), subj))
    try:
        eng = AlertEngine(n, LiveStore(maxlen=5))
        eng.storage_fn = lambda: [{"code": "cold_full", "level": "crit", "title": "Log archiving paused: archive disk full", "body": "b"},
                                  {"code": "cold_80", "level": "warn", "title": "Log archive disk at 81%", "body": "b"}]
        eng.storage_targets_fn = lambda: ["admin@firma.com"]
        eng.evaluate()
        assert sent == [(("admin@firma.com",), "[Watchover] Log archiving paused: archive disk full")]   # built-in rule: critical only
        assert n.alerts()[0]["key"] == "logstore:cold_full"
        eng.evaluate()
        assert len(sent) == 1                                                          # cooldown
        rid = n.add_rule("arşiv", "log_storage", 80, "", "high", "email", "ops@firma.com", 1)
        eng.evaluate()
        assert ("ops@firma.com",) in [s[0] for s in sent] and any("81%" in s[1] for s in sent)   # an own rule at 80 %: warnings too
        assert rid
    finally:
        wn.send_email = orig


def test_demo_purge_reseals_the_archive_it_rewrites(tmp_path):
    st, hot, cold, tiers = build(tmp_path, days=(4, 3))
    tiers.tick(now=NOON)
    tiers.purge("hr-app-01")
    ops = [e["op"] for e in cold.manifest.entries()]
    assert ops.count("reseal") == 2 and la.verify(cold.dir)["ok"]
    assert all(stat.S_IMODE(cold.path(d).stat().st_mode) == 0o440 for d in cold.days())


# ---------------------------------------------------------------- exports for auditors
def test_archive_export_with_its_own_checksum_verifies_offline(tmp_path):
    st, hot, cold, tiers = build(tmp_path)
    tiers.tick(now=NOON)
    days = cold.days()
    res = la.export_archive(cold.dir, days[0], days[1], tmp_path / "exp")
    f = tmp_path / "exp" / res["file"]
    assert res["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest() and res["archives"] == 2 and not res["not_ok"]
    assert (tmp_path / "exp" / (res["file"] + ".sha256")).read_text() == f"{res['sha256']}  {res['file']}\n"
    out = tmp_path / "x"
    with tarfile.open(f) as t:
        t.extractall(out)
    sums = dict(reversed(ln.split("  ")) for ln in (out / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {f"archive/logs-{days[0]}.wcl", f"archive/logs-{days[1]}.wcl", "MANIFEST.jsonl", "MANIFEST.head", "SLICE.json", "README.txt"}
    assert all(hashlib.sha256((out / n).read_bytes()).hexdigest() == h for n, h in sums.items())
    sl = json.loads((out / "SLICE.json").read_text())
    assert [d["status"] for d in sl["days"]] == ["ok", "ok"] and sl["chain_ok"] and len(sl["entries"]) == 2
    assert lt.main(["verify", "--dir", str(out / "archive"), "--manifest", str(out / "MANIFEST.jsonl"), "--from", days[0], "--to", days[1]]) == 0
    os.chmod(out / "archive" / f"logs-{days[0]}.wcl", 0o640)
    (out / "archive" / f"logs-{days[0]}.wcl").write_bytes(b"forged")
    assert lt.main(["verify", "--dir", str(out / "archive"), "--manifest", str(out / "MANIFEST.jsonl"), "--from", days[0], "--to", days[1]]) == 1
    nd = la.export_ndjson(cold, days[0], lt.day_key(NOON), tmp_path / "exp", hot=hot)
    g = tmp_path / "exp" / nd["file"]
    assert nd["sha256"] == hashlib.sha256(g.read_bytes()).hexdigest()
    lines = gzip.decompress(g.read_bytes()).splitlines()
    assert len(lines) == nd["lines"] == len(stored_lines(hot, cold)) and {d["tier"] for d in nd["days"]} == {"cold", "hot"}


def test_audit_jobs_run_one_at_a_time_with_progress(tmp_path):
    import threading
    jobs = la.Jobs(tmp_path / "exports")
    gate = threading.Event()

    def slow(prog, cancel, out):
        prog(0.5)
        gate.wait(5)
        if cancel.is_set():
            raise la.Cancelled()
        return {"x": 1}
    j = jobs.start("verify", {}, "a@b", slow)
    with pytest.raises(la.Busy):
        jobs.start("verify", {}, "a@b", slow)
    import time
    for _ in range(50):
        if jobs.get(j["id"])["progress"] == 0.5:
            break
        time.sleep(0.02)
    assert jobs.get(j["id"])["progress"] == 0.5 and jobs.cancel(j["id"])
    gate.set()
    for _ in range(100):
        if jobs.get(j["id"])["status"] != "running":
            break
        time.sleep(0.02)
    assert jobs.get(j["id"])["status"] == "cancelled"
    assert la.Jobs(tmp_path / "exports").list()[0]["status"] == "cancelled"           # survives a restart
    assert jobs.path("../etc/passwd") is None and jobs.path("watchover-x.tar.part") is None


# ---------------------------------------------------------------- moving a tier
def test_relocate_moves_every_file_without_loss_and_only_then_cleans(tmp_path):
    st, hot, cold, tiers = build(tmp_path)
    tiers.tick(now=NOON)
    snap = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cold.dir.iterdir() if p.is_file()}
    new = tmp_path / "disk2" / "archive"
    r1 = la.relocate(cold.dir, new)
    assert r1["copied"] == len(snap) - 0 or r1["copied"] >= 4
    st.ingest("app.jsonl", payload(TODAY - timedelta(days=1) + timedelta(hours=3), "ea-app-01", 5), agent="ea-app-01")
    tiers.tick(now=NOON + timedelta(days=1))                                           # one more day archived between the passes
    r2 = la.relocate(cold.dir, new, final=True)
    assert 0 < r2["copied"] < r1["copied"] + 3                                          # only what changed
    now_src = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cold.dir.iterdir() if p.is_file() and not p.name.startswith(".")}
    got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in new.iterdir() if p.is_file() and not p.name.startswith(".")}
    assert got == now_src and set(snap) - {".manifest.lock"} <= set(got) | {".manifest.lock"}
    assert la.verify(new)["ok"] and stat.S_IMODE((new / f"logs-{cold.days()[0]}.wcl").stat().st_mode) == 0o440
    c = la.cleanup_source(cold.dir, new)
    assert c["removed"] >= 4 and not [p for p in cold.dir.iterdir() if p.name.endswith(".wcl")] and not (new / la.STATE).exists()
    moved = lt.ColdStore(new)
    assert moved.days() and la.verify(moved.dir)["ok"]


def test_relocate_refuses_a_foreign_directory_and_a_disk_without_room(tmp_path, monkeypatch):
    st, hot, cold, tiers = build(tmp_path, days=(4,))
    tiers.tick(now=NOON)
    home = tmp_path / "home-of-someone"
    home.mkdir()
    (home / "thesis.docx").write_text("mine")
    with pytest.raises(la.AuditError) as e:
        la.relocate(cold.dir, home)
    assert e.value.code == "target_not_empty" and (home / "thesis.docx").read_text() == "mine"
    tight = type("DU", (), {"total": 10 << 30, "used": 10 << 30, "free": 1 << 20})
    monkeypatch.setattr(la.shutil, "disk_usage", lambda p: tight)
    with pytest.raises(la.AuditError) as e:
        la.relocate(cold.dir, tmp_path / "small")
    assert e.value.code == "no_space" and not [p for p in (tmp_path / "small").iterdir() if p.name.endswith(".wcl")]
    assert lt.main(["relocate", str(cold.dir), str(tmp_path / "small")]) == 3


def test_path_rules():
    for p in ("/srv/watchover/hot", "/data2/arsiv", "/mnt/nvme0/wo-hot", "/var/lib/watchover/cold", "/Users/me/wo/cold"):
        assert la.check_path(p) is None, p
    for p, why in (("srv/x", "not_absolute"), ("/", "system_dir"), ("/etc", "system_dir"), ("/etc/wo", "system_dir"), ("/srv", "system_dir"),
                   ("/var/lib/docker/volumes/x", "system_dir"), ("/srv/a b", "bad_chars"), ("/srv/../etc", "bad_chars"), ("/srv/x;rm", "bad_chars")):
        assert la.check_path(p) == why, p
    assert la.check_pair("/srv/wo", "/srv/wo/cold") == "nested" and la.check_pair("/srv/a", "/srv/a/") == "same_dir"
    assert la.check_pair("/srv/hot", "/srv/cold") is None


def test_estimate_from_the_ingest_rate():
    cold = {"days": [{"day": f"2026100{i}", "lines": 144_000_000} for i in range(1, 9)], "lines": 1_152_000_000, "bytes_per_line": 7.6, "bytes": 8_755_200_000}
    e = la.estimate(cold, {}, 365)
    assert e["lines_per_min"] == 100_000 and e["basis"] == "cold" and 1.0e9 < e["daily_bytes"] < 1.2e9 and 380e9 < e["yearly_bytes"] < 420e9
    h = la.estimate({"days": [], "lines": 0}, {"events": 6_000_000, "oldest": "2026-10-16T00:00:00+00:00", "newest": "2026-10-16T01:00:00+00:00"}, 365)
    assert h["basis"] == "hot" and h["lines_per_min"] == 100_000 and h["bytes_per_line"] == la.LINE_BYTES and not h["bpl_measured"]


# ---------------------------------------------------------------- HTTP: permissions, step-up, config by mode, jobs, downloads
def test_storage_http_permissions_config_jobs_and_download(tmp_path, monkeypatch):
    import time
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    for k in ("DATABASE_URL", "WATCHOVER_LOG_STORE", "WATCHOVER_LOG_HOT_DIR", "WATCHOVER_LOG_COLD_DIR", "WATCHOVER_DOCKER", "WATCHOVER_UPDATES_DIR",
              "KUBERNETES_SERVICE_HOST", "WATCHOVER_SETTINGS", "WATCHOVER_LOG_HOT_PATH", "WATCHOVER_LOG_COLD_PATH"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover import settings as wo_settings
    from watchover.updater import Updater
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("viewer@firma.com", "Str0ng!Passw0rd", role="viewer")
    svc.live.retention_h = 24 * 30
    for d in (4, 3):
        svc.live.ingest("app.jsonl", payload(TODAY - timedelta(days=d) + timedelta(hours=9), "ea-app-01", 10), agent="ea-app-01")
    svc.log_tiers.tick(now=NOON)
    days = svc.log_cold.days()
    assert len(days) == 2
    with TestClient(a) as c:
        adm = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Test-Yonetici-Parola-42"}).json()["token"]}
        vw = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": "viewer@firma.com", "password": "Str0ng!Passw0rd"}).json()["token"]}
        assert c.put("/api/system/storage/config", json={"cold_days": 30}, headers=vw).status_code == 403
        assert c.post("/api/system/storage/jobs", json={"kind": "verify"}, headers=vw).status_code == 403
        assert c.get("/api/system/storage/jobs", headers=vw).status_code == 403
        d = c.get("/api/system/storage", headers=adm).json()
        assert d["setup"]["mode"] == "inprocess" and d["setup"]["paths"]["cold"]["configured"] is False and d["manifest"]["head"]["seq"] == 2
        assert d["cold_retention_days"] == 365 and d["estimate"]["basis"] in ("cold", "hot")
        r = c.put("/api/system/storage/config", json={"cold_days": 400}, headers=adm)
        assert r.status_code == 428 and r.json()["detail"]["code"] == "act_stepup_required"            # a fresh step-up first
        assert c.post("/api/auth/stepup/verify", json={"password": "Test-Yonetici-Parola-42"}, headers=adm).status_code == 200
        r = c.put("/api/system/storage/config", json={"cold_path": "/etc/watchover"}, headers=adm)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "stg_path_system_dir"
        r = c.put("/api/system/storage/config", json={"hot_path": "/srv/wo", "cold_path": "/srv/wo/arsiv"}, headers=adm)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "stg_path_nested"
        r = c.put("/api/system/storage/config", json={"cold_days": 400, "hot_hours": 72}, headers=adm)          # numbers alone: saved
        assert r.json()["applied"] and wo_settings.load()["cold_retention_days"] == 400 and svc.live.retention_h == 72
        # Docker with the updater: a request of kind "storage" the updater accepts as it is
        spool = tmp_path / "spool"
        (spool / "requests").mkdir(parents=True)
        monkeypatch.setenv("WATCHOVER_DOCKER", "1")
        monkeypatch.setenv("WATCHOVER_UPDATES_DIR", str(spool))
        (spool / "updater.json").write_text(json.dumps({"ts": time.time()}))
        r = c.put("/api/system/storage/config", json={"hot_path": "/srv/wo/hot", "cold_path": "/data2/wo/arsiv", "cold_days": 365}, headers=adm)
        assert r.status_code == 200 and r.json()["queued"] and "storage set --hot /srv/wo/hot --cold /data2/wo/arsiv --cold-days 365" in r.json()["command"]
        req = json.loads((spool / "requests" / f"{r.json()['queued']}.json").read_text())
        assert req["kind"] == "storage" and req["user"] == "admin@watchover.local"
        inst = tmp_path / "install"; inst.mkdir()
        assert Updater(inst, spool).clean(req, req["id"])["cold"] == "/data2/wo/arsiv"
        r = c.put("/api/system/storage/config", json={"cold_path": "/data3/arsiv"}, headers=adm)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "upd_busy"                       # one job at a time
        monkeypatch.setenv("WATCHOVER_UPDATES_DIR", str(tmp_path / "no-updater"))                         # Docker without the updater
        r = c.put("/api/system/storage/config", json={"cold_path": "/data3/arsiv"}, headers=adm)
        assert r.json() == {"applied": False, "mode": "manual", "command": "./watchover.sh storage set --cold /data3/arsiv"}
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")                                         # OpenShift: Helm values only
        r = c.put("/api/system/storage/config", json={"cold_path": "/data3/arsiv", "cold_days": 365}, headers=adm)
        assert not r.json()["applied"] and "--set retention.coldRetentionDays=365" in r.json()["helm"][0] and "persistence.logsCold.size" in r.json()["helm"][0]
        for k in ("KUBERNETES_SERVICE_HOST", "WATCHOVER_DOCKER", "WATCHOVER_UPDATES_DIR"):
            monkeypatch.delenv(k)
        # jobs: verify, then an archive export downloaded with a ticket, its checksum in a header
        j = c.post("/api/system/storage/jobs", json={"kind": "verify"}, headers=adm)
        assert j.status_code == 202

        def wait(jid):
            for _ in range(200):
                s = c.get(f"/api/system/storage/jobs/{jid}", headers=adm).json()
                if s["status"] != "running":
                    return s
                time.sleep(0.03)
            raise AssertionError("job did not finish")
        s = wait(j.json()["id"])
        assert s["status"] == "done" and s["result"]["ok"] and s["result"]["checked"] == 2
        assert la.last_verify(svc.log_cold.dir)["ok"] and la.last_verify(svc.log_cold.dir)["user"] == "admin@watchover.local"
        j = c.post("/api/system/storage/jobs", json={"kind": "export", "start": f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:]}", "end": days[1], "format": "archive"}, headers=adm)
        s = wait(j.json()["id"])
        assert s["status"] == "done" and s["result"]["archives"] == 2, s
        name = s["result"]["file"]
        assert c.get(f"/api/system/storage/exports/{name}").status_code == 403                          # never without a ticket
        tk = c.post(f"/api/system/storage/exports/{name}/ticket", headers=adm).json()
        f = c.get(tk["url"])
        assert f.status_code == 200 and hashlib.sha256(f.content).hexdigest() == f.headers["x-content-sha256"] == s["result"]["sha256"]
        j = c.post("/api/system/storage/jobs", json={"kind": "search", "start": days[0], "end": lt.day_key(NOON), "query": "level:error"}, headers=adm)
        s = wait(j.json()["id"])
        assert s["status"] == "done" and s["result"]["matched"] == 6 and s["result"]["cold_rows"] > 0, s     # 2 days x 10 lines, every 4th an error
        tk = c.post(f"/api/system/storage/exports/{s['result']['file']}/ticket", headers=adm).json()
        rows = [json.loads(x) for x in gzip.decompress(c.get(tk["url"]).content).splitlines()]
        assert len(rows) == 6 and all(r["level"] == "ERROR" and r["stored"]["msg"] == r["message"] for r in rows)
        assert c.post("/api/system/storage/jobs", json={"kind": "search", "start": days[0]}, headers=adm).status_code == 422
        ev = [r["detail"] for r in svc.kb._exec("SELECT detail FROM auth_events WHERE event = 'sys.storage' ORDER BY id")]
        assert any("audit verify" in x for x in ev) and any("audit export" in x and "download" not in x for x in ev) and any("storage retention" in x for x in ev)
