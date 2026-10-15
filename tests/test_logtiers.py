"""Log tiers (logtiers.py): hot SQLite day files off the main database, the cold day archive (template + digit runs, lzma),
search / line / context across the hot-cold boundary, size caps, a crash mid-archive, team scope on archived lines, migration
from the old main-database table, and the storage report over HTTP."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from watchover import logstore
from watchover import logtiers as lt
from watchover.db import Database
from watchover.live import LiveStore
from watchover.logsearch import LogSearch
from watchover.teams import Scope

UTC = timezone.utc
TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
NOON = TODAY + timedelta(hours=12)                     # the job's clock: day-3 and older are past 48 h, day-2 is not
SECRET = "hunter2"
CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "tiers-test")
    import watchover.privacy as pv
    monkeypatch.setattr(pv, "_KEY", None)
    logstore._SCRUB.clear()


def payload(t0, host, n, svc="ea-portal", step_s=60):
    recs = []
    for i in range(n):
        r = {"ts": (t0 + timedelta(seconds=i * step_s, milliseconds=i * 7)).isoformat(), "level": "error" if i % 5 == 0 else "info", "service": svc,
             "host": host, "env": "prod", "msg": f"order {100000 + i} processed in {i * 7} ms by {host}", "trace_id": f"{i + 1:032x}",
             "user": f"user{i}@corp.com", "status": 500 if i % 5 == 0 else 200}
        if i == 3:
            r["msg"] = f"login failed password={SECRET} card {CARD}"
            r["password"] = SECRET
        recs.append(r)
    return ("\n".join(json.dumps(x) for x in recs) + "\n").encode()


def build(tmp_path, days=(4, 3, 2, 1), per=25):
    hot, cold = lt.HotStore(tmp_path / "hot"), lt.ColdStore(tmp_path / "cold")
    st = LiveStore(maxlen=5)
    st.retention_h = 24 * 30                           # lines days old are stored by their stamp (not as arrivals)
    st.attach_db(hot)
    st.cold = cold
    for d in days:
        for h in ("ea-app-01", "hr-app-01"):
            st.ingest("app.jsonl", payload(TODAY - timedelta(days=d) + timedelta(hours=10), h, per), agent=h)
    for h in ("ea-app-01", "hr-app-01"):
        st.ingest("app.jsonl", payload(datetime.now(UTC) - timedelta(minutes=30), h, 5, step_s=1), agent=h)
    tiers = lt.LogTiers(hot, cold, lambda: {"live_retention_h": 48, "cold_retention_days": 7}, subprocess_ok=False)
    return st, hot, cold, tiers, LogSearch(st)


def hot_lines(hot) -> dict:
    """line id -> (row fields, stored JSON line) for every stored line."""
    out = {}
    for r in hot._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches"):
        for i, ln in enumerate(logstore.row_bytes(r).split(b"\n")):
            if ln:
                out[logstore.line_id(json.loads(ln), r["id"], i)] = ({k: r[k] for k in lt.COLS if k not in ("pz", "payload")}, ln)
    return out


def all_pages(ls, q, s, e, limit=40, scope=None):
    ids, cur, srcs = [], None, set()
    for _ in range(200):
        r = ls.search(q, start=s.isoformat(), end=e.isoformat(), limit=limit, cursor=cur, scope=scope)
        ids += [int(x["id"]) for x in r["lines"]]
        srcs.add(r["source"])
        cur = r["next"]
        if not cur:
            return ids, r, srcs
    raise AssertionError("too many pages")


# ---------------------------------------------------------------- hot day files
def test_hot_rotation_routing_and_expiry(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path, days=(3, 1), per=5)
    days = hot.days()
    assert days == sorted({lt.day_key(TODAY - timedelta(days=3)), lt.day_key(TODAY - timedelta(days=1)), lt.day_key(datetime.now(UTC))} & set(days)) and len(days) >= 3
    assert all((tmp_path / "hot" / f"logs-{d}.db").exists() for d in days)
    for r in hot._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches"):          # every row sits in the file of its ts_max day
        assert any(lt.day_of(r["ts_max"]) == d for d in days)
    ids = [r["id"] for r in hot._exec("SELECT id FROM live_batches")]
    assert len(ids) == len(set(ids))                                               # one id counter over every file
    # a stamp days ahead lands in tomorrow's file at most; a closed (archiving) day's late rows go to the arrival day
    fut = (datetime.now(UTC) + timedelta(days=5)).isoformat(timespec="seconds")
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    hot.executemany(logstore.INSERT_V2, [(fut, fut, now_iso, "a", "", 1, "", 2, b"", 0, 0, 2, "h", "s")])
    assert lt.day_key(datetime.now(UTC) + timedelta(days=1)) in hot.days()
    old_day = days[0]
    hot.close_day(old_day)
    late = (TODAY - timedelta(days=3) + timedelta(hours=11)).isoformat(timespec="seconds")
    hot.executemany(logstore.INSERT_V2, [(late, late, now_iso, "a", "", 1, "", 2, b"", 0, 0, 2, "h", "s")])
    assert hot._exec("SELECT ts_max FROM live_batches WHERE ts_max = ?", (late,))   # still readable, written to today's file
    # expiry without an archive: whole files go; ids and line ids never restart (high-water marks in meta.db)
    seq0, idmax = hot.last_seq(), max(r["id"] for r in hot._exec("SELECT id FROM live_batches"))
    hot.closed.clear()
    tiers0 = lt.LogTiers(hot, None, lambda: {"live_retention_h": 48, "cold_retention_days": 0}, subprocess_ok=False)
    out = tiers0.tick(now=NOON)
    assert old_day in out["dropped_hot"] and not (tmp_path / "hot" / f"logs-{old_day}.db").exists() and old_day not in hot.days()
    hot.close()
    again = lt.HotStore(tmp_path / "hot")
    assert again.last_seq() >= seq0 and again._next_id > idmax
    st2 = LiveStore(maxlen=5); st2.attach_db(again)
    assert st2.seq >= seq0
    # LiveStore.prune expires whole days when no archive owns them
    st2.retention_h = 0
    assert st2.prune(datetime.now(UTC) + timedelta(days=3)) > 0 and again.days() == []
    again.close()


# ---------------------------------------------------------------- cold archive
def test_cold_roundtrip_every_field_and_masking(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path)
    before = hot_lines(hot)
    out = tiers.tick(now=NOON)
    archived = {a["day"] for a in out["archived"]}
    assert archived == {lt.day_key(TODAY - timedelta(days=4)), lt.day_key(TODAY - timedelta(days=3))} and not out["errors"]
    assert set(cold.days()) == archived and not set(hot.days()) & archived
    rows = [r for d in cold.days() for r in cold.iter_day(d)]
    got = {}
    for r in rows:
        for i, ln in enumerate(r["_text"].split(b"\n")):
            if ln:
                got[logstore.line_id(json.loads(ln), r["id"], i)] = ({k: r[k] for k in lt.COLS if k not in ("pz", "payload")}, ln)
    moved = {k: v for k, v in before.items() if lt.day_of(v[0]["ts_max"]) in archived}
    assert moved and got == moved                                                   # every row field and every byte of every line
    blob = b"".join(r["_text"] for r in rows)
    secret = [json.loads(ln) for _r, ln in got.values() if b'"password"' in ln]   # what the hot tier masks stays masked
    assert secret and all(d["at"]["password"] == "***" and SECRET not in d["raw"] and CARD not in d["raw"] and d["at"]["user"].startswith("u_")
                          for d in secret)
    assert b"user3@corp.com" not in blob and b"[KART]" in blob
    lid = next(k for k, (_r, ln) in got.items() if b'"password"' in ln)
    det = LogSearch(st).line(lid)                                                    # the explorer shows the message scrubbed
    assert det["source_of_detail"] == "cold" and SECRET not in json.dumps(det) and CARD not in json.dumps(det)
    assert all(a["bytes_per_line"] and a["lines"] for a in out["archived"])
    rep = tiers.report()
    assert rep["cold"]["lines"] == len(moved) and rep["cold"]["bytes"] > 0 and rep["hot"]["bytes"] > 0 and not rep["warnings"]


def test_search_line_context_across_the_boundary(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path)
    s, e = TODAY - timedelta(days=5), datetime.now(UTC) + timedelta(minutes=1)
    ids0, r0, _ = all_pages(ls, "", s, e)
    day4 = sorted(k for k, v in hot_lines(hot).items() if lt.day_of(v[0]["ts_max"]) == lt.day_key(TODAY - timedelta(days=4)))
    old_lid = day4[len(day4) // 4]                                                 # mid-day of one host
    det0, ctx0 = ls.line(old_lid), ls.context(old_lid, 5, 5)
    tiers.tick(now=NOON)
    ls._cache.clear()
    ids1, r1, srcs = all_pages(ls, "", s, e)
    assert ids1 == ids0 and len(ids1) == len(set(ids1)) and "cold" in srcs     # same lines, same order, once each
    det1, ctx1 = ls.line(old_lid), ls.context(old_lid, 5, 5)
    assert det1.pop("source_of_detail") == "cold" and det0.pop("source_of_detail") == "stored" and det1 == det0
    assert ctx1 == ctx0 and ctx1["before"] and ctx1["after"]
    assert det1["message"].count("order") == 1
    # a text search over archived days: cold source, the cursor pages on
    old_s, old_e = TODAY - timedelta(days=5), TODAY - timedelta(days=2)
    r = ls.search("processed", start=old_s.isoformat(), end=old_e.isoformat(), limit=10)
    assert r["source"] == "cold" and len(r["lines"]) == 10 and r["next"] and r["scan"]["cold"] > 0
    ids, last, _ = all_pages(ls, "processed", old_s, old_e, limit=10)
    want = sorted((k for k, v in hot_lines_snapshot(cold).items() if b"processed" in v), reverse=False)
    assert sorted(ids) == want and last["total"]["value"] >= 0
    # the range is inside the cold retention: not "expired"; the retention says how far back the archive goes
    ret = r["retention"]
    assert ret["cold"]["days"] == 2 and ret["cold"]["oldest"].startswith((TODAY - timedelta(days=4)).date().isoformat())
    assert ls.search("", start=(TODAY - timedelta(days=9)).isoformat(), end=(TODAY - timedelta(days=8)).isoformat())["source"] == "history"
    # facets, groups and a text histogram over archived days scan the archive too
    assert ls.facets("processed", start=old_s.isoformat(), end=old_e.isoformat())["source"] == "cold"
    assert ls.groups("", start=old_s.isoformat(), end=old_e.isoformat())["source"] == "cold"
    assert ls.histogram("processed", start=old_s.isoformat(), end=old_e.isoformat())["source"] == "cold"


def test_older_hot_day_does_not_hide_archived_days(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path, days=(4, 3))
    tiers.tick(now=NOON)
    st.ingest("app.jsonl", payload(TODAY - timedelta(days=6) + timedelta(hours=9), "ea-app-01", 5), agent="ea-app-01")   # a hot day older than the archive
    assert hot.days()[0] == lt.day_key(TODAY - timedelta(days=6)) and len(cold.days()) == 2
    want = set(hot_lines(hot)) | set(hot_lines_snapshot(cold))
    ids, r, srcs = all_pages(ls, "", TODAY - timedelta(days=7), datetime.now(UTC) + timedelta(minutes=1))
    assert set(ids) == want and len(ids) == len(want) and "cold" in srcs
    assert ids == sorted(ids, key=lambda i: (ls.line(i)["ts"], i), reverse=True)    # newest first across hot and cold days
    lid = next(iter(hot_lines_snapshot(cold)))
    assert ls.line(lid)["source_of_detail"] == "cold"


def test_chunk_prefilter_never_drops_a_matching_line(tmp_path):
    import random
    from watchover import logquery as lq
    st, hot, cold, tiers, ls = build(tmp_path, days=(2,), per=40)
    rows = [dict(r, _text=logstore.row_bytes(r)) for r in hot._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches")]
    blob, _n, _b = lt.encode_chunk(rows)
    sec = lt._sections(__import__("lzma").decompress(blob))
    assert [r["_text"] for r in lt.decode_sections(sec)] == [r["_text"] for r in rows]
    rng = random.Random(5)
    lines = [ln for r in rows for ln in r["_text"].split(b"\n") if ln]
    for _ in range(3000):                                                           # any piece of any stored line: never ruled out
        ln = rng.choice(lines).decode()
        a = rng.randrange(len(ln) - 2)
        piece = ln[a:a + rng.randint(2, 24)]
        pf = lq.Prefilter(piece)
        assert lt.chunk_may_hold(sec, pf.lit, pf.fold), piece
    for absent in ("zqxjv", "98765432109876", "order 99999999"):
        pf = lq.Prefilter(absent)
        assert not lt.chunk_may_hold(sec, pf.lit, pf.fold)


def hot_lines_snapshot(cold) -> dict:
    out = {}
    for d in cold.days():
        for r in cold.iter_day(d):
            for i, ln in enumerate(r["_text"].split(b"\n")):
                if ln:
                    out[logstore.line_id(json.loads(ln), r["id"], i)] = ln
    return out


def test_team_scope_on_cold_results(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path)
    tiers.tick(now=NOON)
    ea = Scope({"ea-app-01"}, [], ["EA"])
    old_s, old_e = TODAY - timedelta(days=5), TODAY - timedelta(days=2)
    ids, r, srcs = all_pages(ls, "", old_s, old_e, scope=ea)
    assert ids and srcs == {"cold"}
    for lid in ids:
        assert ls.line(lid, scope=ea)["host"] == "ea-app-01"
    other = next(k for k, v in hot_lines_snapshot(cold).items() if b'"host":"hr-app-01"' in v)
    with pytest.raises(KeyError):
        ls.line(other, scope=ea)
    with pytest.raises(KeyError):
        ls.context(other, scope=ea)
    f = ls.facets("", start=old_s.isoformat(), end=old_e.isoformat(), scope=ea)
    assert f["source"] == "cold" and {x["v"] for x in f["facets"]["host"]["values"]} == {"ea-app-01"}


# ---------------------------------------------------------------- caps, crash, retention
def test_size_caps_and_cold_retention(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path)
    n_lines = len(hot_lines(hot))
    capped = lt.LogTiers(hot, cold, lambda: {"live_retention_h": 48, "cold_retention_days": 7, "hot_max_gb": 1e-9}, subprocess_ok=False)
    out = capped.tick(now=NOON)
    today = lt.day_key(NOON)
    assert all(d >= today for d in hot.days()) and len(out["archived"]) == 4 and {a["reason"] for a in out["archived"]} == {"retention", "hot_cap"}
    assert len(hot_lines(hot)) + len(hot_lines_snapshot(cold)) == n_lines          # moved, not lost
    rep = capped.report()
    assert rep["hot"]["pct"] > 85 and any(w["code"] == "hot_cap" for w in rep["warnings"])
    c2 = lt.LogTiers(hot, cold, lambda: {"live_retention_h": 48, "cold_retention_days": 7, "cold_max_gb": 1e-9}, subprocess_ok=False)
    out = c2.tick(now=NOON)
    assert len(cold.days()) == 1 and len(out["deleted_cold"]) == 3                 # oldest days first, the newest one kept
    c3 = lt.LogTiers(hot, cold, lambda: {"live_retention_h": 48, "cold_retention_days": 7}, subprocess_ok=False)
    assert c3.tick(now=NOON + timedelta(days=30))["deleted_cold"] and cold.days() == []


def test_crash_mid_archive_no_loss_no_duplicate(tmp_path, monkeypatch):
    st, hot, cold, tiers, ls = build(tmp_path, per=60)
    before = hot_lines(hot)
    real = lt.archive_file
    monkeypatch.setattr(lt, "archive_file", lambda p, d, day, *a, **k: real(p, d, day, chunk_lines=20, _fail_after=1))
    out = tiers.tick(now=NOON)
    assert out["errors"] and not out["archived"] and not cold.days()                # the temp file never became an archive
    assert list((tmp_path / "cold").glob("*.wcl.tmp")) and hot_lines(hot) == before   # nothing left the hot tier
    monkeypatch.setattr(lt, "archive_file", lambda p, d, day, *a, **k: real(p, d, day, chunk_lines=20))
    day = lt.day_key(TODAY - timedelta(days=4))
    real(hot.files[day], cold.dir, day, chunk_lines=20)                             # a crash after the rename, before the hot delete
    out = tiers.tick(now=NOON)
    assert not out["errors"] and len(out["archived"]) == 2
    got = hot_lines_snapshot(cold)
    moved = {k: v[1] for k, v in before.items() if lt.day_of(v[0]["ts_max"]) in set(cold.days())}
    assert got == moved                                                              # merged by row id: each line once
    assert sum(r["n"] for d in cold.days() for r in cold.iter_day(d)) == len(moved)
    tiers.tick(now=NOON + timedelta(minutes=5))
    assert not list((tmp_path / "cold").glob("*.wcl.tmp"))                          # temp leftovers are cleaned


def test_archive_in_a_child_process(tmp_path):
    st, hot, cold, tiers, ls = build(tmp_path, days=(4,), per=10)
    tiers.subprocess_ok = True
    out = tiers.tick(now=NOON)
    assert len(out["archived"]) == 1 and out["archived"][0]["lines"] == 20 and cold.days()


# ---------------------------------------------------------------- migration from the main database
def test_migration_from_the_old_table(tmp_path):
    kb = Database(str(tmp_path / "k.db"))
    old = LiveStore(maxlen=5)
    old.retention_h = 24 * 30
    old.attach_db(kb)
    for h in ("ea-app-01", "hr-app-01"):
        old.ingest("app.jsonl", payload(datetime.now(UTC) - timedelta(hours=3), h, 30), agent=h)
    ls_old = LogSearch(old, kb=kb)
    s, e = datetime.now(UTC) - timedelta(hours=4), datetime.now(UTC) + timedelta(minutes=1)
    ids0, _, _ = all_pages(ls_old, "", s, e)
    rows0 = {r["id"]: r for r in kb._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches")}
    hot = lt.HotStore(tmp_path / "hot")
    assert lt.LogTiers.prepare(kb, hot)
    first = kb._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches ORDER BY id LIMIT 3")
    hot.insert_rows(first)                                                           # an interrupted earlier run copied these already
    st = LiveStore(maxlen=5); st.attach_db(hot)
    assert st.seq >= old.seq
    tiers = lt.LogTiers(hot, lt.ColdStore(tmp_path / "cold"), lambda: {}, subprocess_ok=False)
    m = tiers.migrate(kb, batch=4, pause=0)
    assert m["state"] == "done" and m["copied"] == len(rows0) and "live_batches" not in kb.tables()
    got = {r["id"]: r for r in hot._exec(f"SELECT {logstore.ROW_COLS} FROM live_batches")}
    assert set(got) == set(rows0) and all(logstore.row_bytes(got[k]) == logstore.row_bytes(rows0[k]) for k in rows0)
    ids1, _, _ = all_pages(LogSearch(st), "", s, e)
    assert ids1 == ids0
    st.ingest("app.jsonl", payload(datetime.now(UTC), "ea-app-01", 2), agent="ea-app-01")
    new = hot._exec("SELECT id, seq_min FROM live_batches ORDER BY id DESC LIMIT 1")[0]
    assert new["id"] > max(rows0) and new["seq_min"] > max(r["seq_max"] for r in rows0.values())


# ---------------------------------------------------------------- the HTTP layer
def test_storage_endpoint_and_files_mode(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    for k in ("DATABASE_URL", "WATCHOVER_LOG_STORE", "WATCHOVER_LOG_HOT_DIR", "WATCHOVER_LOG_COLD_DIR"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as c:
        svc.live.ingest("app.jsonl", payload(datetime.now(UTC) - timedelta(minutes=5), "ea-app-01", 4, step_s=1), agent="ea-app-01")
        assert "live_batches" not in svc.kb.tables()                                 # raw lines never touch the main database
        assert (tmp_path / "home" / "logs" / "hot").is_dir() and list((tmp_path / "home" / "logs" / "hot").glob("logs-*.db"))
        tok = c.post("/api/auth/login", json={"email": "admin@watchover.local", "password": "Watchover!Admin2026"}).json()["token"]
        r = c.get("/api/system/storage", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["mode"] == "files" and d["hot"]["events"] == 4 and d["hot"]["days"] and d["cold"]["bytes"] == 0
        assert d["main_db"]["backend"] == "sqlite" and d["main_db"]["bytes"] > 0 and d["cold_retention_days"] == 7
        assert c.get("/api/system/storage").status_code == 401


# ---------------------------------------------------------------- demo clean-up (demo.PURGERS)
def test_demo_purger_removes_simulator_lines_from_both_tiers(tmp_path):
    from watchover import demo
    st, hot, cold, tiers, ls = build(tmp_path, days=(4, 3, 1), per=10)
    for d in (4, 1):
        st.ingest("sim.jsonl", payload(TODAY - timedelta(days=d) + timedelta(hours=11), "prd-api-01", 8, svc="sim"), agent="simulator")
    tiers.tick(now=NOON)                                                            # day-4 (with simulator lines) and day-3 archived
    real_before = {k: v for k, v in {**{k: v[1] for k, v in hot_lines(hot).items()}, **hot_lines_snapshot(cold)}.items() if b'"agent":"simulator"' not in v}
    lt.register_purger(tiers)
    assert lt.demo_purge in demo.PURGERS and demo.PURGERS.count(lt.demo_purge) == 1
    assert lt.demo_purge("simulator", {"prd-api-01"}, True) == {"live_lines_hot": 8, "live_lines_cold": 8}   # dry run: counted only
    assert sum(b'"agent":"simulator"' in v for v in hot_lines_snapshot(cold).values()) == 8
    day3 = lt.day_key(TODAY - timedelta(days=3))
    size3 = cold.path(day3).stat().st_mtime_ns
    assert lt.demo_purge("simulator", {"prd-api-01"}, False) == {"live_lines_hot": 8, "live_lines_cold": 8}
    after = {**{k: v[1] for k, v in hot_lines(hot).items()}, **hot_lines_snapshot(cold)}
    assert not any(b'"agent":"simulator"' in v for v in after.values()) and after == real_before   # real lines untouched, byte for byte
    assert cold.path(day3).stat().st_mtime_ns == size3                              # a day without simulator lines is not rewritten
    assert cold.index(lt.day_key(TODAY - timedelta(days=4)))["agents"] == ["ea-app-01", "hr-app-01"]
    assert lt.demo_purge("simulator", set(), False) == {"live_lines_hot": 0, "live_lines_cold": 0}
    ids, _r, _s = all_pages(ls, "", TODAY - timedelta(days=5), datetime.now(UTC) + timedelta(minutes=1))
    assert set(ids) == set(real_before)


def test_demo_clean_reaches_the_tiers(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    for k in ("DATABASE_URL", "WATCHOVER_LOG_STORE", "WATCHOVER_LOG_HOT_DIR", "WATCHOVER_LOG_COLD_DIR"):
        monkeypatch.delenv(k, raising=False)
    from watchover import demo
    from watchover.api import create_app
    svc = create_app().state.services
    svc.live.ingest("sim.jsonl", payload(datetime.now(UTC) - timedelta(minutes=5), "prd-api-01", 6, step_s=1), agent="simulator")
    svc.live.ingest("app.jsonl", payload(datetime.now(UTC) - timedelta(minutes=5), "ea-app-01", 3, step_s=1), agent="ea-app-01")
    dry = demo.clean(svc, dry_run=True)
    assert dry["counts"]["live_lines_hot"] == 6 and dry["counts"]["live_lines_cold"] == 0 and not dry["pending"]
    done = demo.clean(svc)
    assert done["counts"]["live_lines_hot"] == 6 and not done["pending"]
    left = svc.log_hot._exec("SELECT agent, SUM(n) AS n FROM live_batches GROUP BY agent")
    assert left == [{"agent": "ea-app-01", "n": 3}]
    svc.close()
