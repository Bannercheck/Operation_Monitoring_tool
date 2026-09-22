"""Phase 1 at scale: the live window survives the ring buffer (database batches), retention pruning, capacity figures, settings."""
import random
from datetime import datetime, timedelta, timezone

from watchover.knowledge import Knowledge
from watchover.live import LiveStore, simulate_batch

UTC = timezone.utc


def _fill(store, batches=6, n=40, seed=1):
    rng = random.Random(seed)
    for i in range(batches):
        store.ingest("sim.jsonl", simulate_batch(rng, n, i % 2 == 0), agent="sim", env="prod")


def test_window_served_from_database_when_ring_is_short(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    ring_only = LiveStore(maxlen=100); _fill(ring_only)
    since = datetime.now(UTC) - timedelta(minutes=30)
    assert len(ring_only.snapshot(since=since)) <= 100                    # the old behaviour: the ring is all there is
    store = LiveStore(spool=tmp_path / "live.jsonl", maxlen=100); store.attach_db(kb); _fill(store)
    total = store.received - len(store.metrics)
    got = store.snapshot(since=since)
    assert len(got) == total > 100 and store.db_served == 1 and store.last_db_window[1] == total
    assert all(o.attributes.get("agent") == "sim" and o.environment == "prod" for o in got)
    assert {o.severity for o in got} & {"ERROR", "INFO", "WARN", "WARNING"}
    # a filter still works on the database window; a window the ring covers stays in memory
    assert store.snapshot(env="prod", since=since) and store.db_served == 2
    covered = store.buf[0].timestamp                                       # the ring reaches back to this instant: memory, not the database
    assert len(store.snapshot(since=covered)) == 100 and store.db_served == 2
    # cap: the newest N are kept
    store.window_max_events = 50
    assert len(store.snapshot(since=since)) == 50


def test_window_survives_a_restart_and_prunes(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    a = LiveStore(maxlen=100); a.attach_db(kb); _fill(a, batches=3)
    b = LiveStore(maxlen=100); b.attach_db(kb)                             # "restart": empty ring, same database
    since = datetime.now(UTC) - timedelta(minutes=30)
    assert len(b.snapshot(since=since)) == a.received - len(a.metrics) and b.db_stats()["batches"] == 3
    b.retention_h = 0
    assert b.prune(datetime.now(UTC) + timedelta(seconds=5)) == 3 and b.db_stats()["batches"] == 0
    assert LiveStore().prune() == 0


def test_capacity_report_and_warnings(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    store = LiveStore(spool=tmp_path / "live.jsonl", maxlen=100); store.attach_db(kb); _fill(store)
    c = store.capacity(window_min=5)
    assert c["events_per_s"] > 0 and c["ring"]["events"] == 100 and c["ring"]["fill_pct"] == 100.0
    assert c["db"]["enabled"] and c["db"]["batches"] == 6 and c["db"]["events"] == store.received - len(store.metrics) and c["db"]["bytes"] > 0
    assert c["disk"] and c["disk"]["total"] > 0 and c["spool_bytes"] > 0 and c["retention_h"] == 48
    # a full ring spanning seconds cannot cover a 5-minute window: served from the database, said so
    assert not c["ring"]["covers_window"] and any(w["code"] == "ring_short" for w in c["warnings"])
    empty = LiveStore(maxlen=100).capacity()
    assert empty["events_per_s"] == 0 and empty["ring"]["covers_window"] and not empty["warnings"] and empty["db"] == {"enabled": False}
