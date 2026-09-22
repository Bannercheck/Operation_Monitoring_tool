"""Corporate memory: hash embeddings without a model, feeds (anomaly, resolution, live window), re-indexing after a model switch."""
from pathlib import Path

from watchover import analysis as an
from watchover.knowledge import HASH_MODEL, Knowledge, cosine, hash_embed
from watchover.memory import Memory
from watchover.pipeline import ingest_path

ROOT = Path(__file__).resolve().parents[1]


def test_hash_embedding_is_deterministic_and_semantic():
    a, b = hash_embed("postgres connection timeout on db-01"), hash_embed("connection timeout to postgres db-01")
    assert a == hash_embed("postgres connection timeout on db-01") and len(a) == 256
    assert cosine(a, b) > 0.5 > cosine(a, hash_embed("disk usage 97% on nas-02"))


def test_feeds_dedupe_and_search_without_a_model(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    mem = Memory(kb, live=None, interval_min=0)
    rec = {"id": 7, "key": "storm:prod:web-01", "kind": "error_storm", "title": "Error storm on web-01", "detail": "errors 40/min vs baseline 4/min", "env": "prod", "host": "web-01", "service": "api", "observed": 40, "baseline": 4, "score": 6.1, "first_seen": "2026-09-22T10:00"}
    lid = mem.remember_anomaly(rec)
    assert lid and mem.remember_anomaly({**rec, "observed": 55}) == lid
    row = kb.get(lid)
    assert row["kind"] == "anomaly" and row["occurrences"] == 2 and "55" in row["text"] and row["embed_model"] == HASH_MODEL
    assert mem.remember_action({"id": 3, "status": "open", "title": "Restart api"}) is None
    rid = mem.remember_action({"id": 3, "status": "done", "title": "Restart api pods on web-01", "incident_id": "INC-1", "priority": "P1", "owner": "ali", "recommendation": "rollout restart", "evidence": "", "updated_at": "2026-09-22T11:00"})
    assert kb.get(rid)["kind"] == "resolution"
    hits = kb.search("error storm web-01")
    assert hits and hits[0]["id"] == lid
    st = mem.stats()
    assert st["total"] == 2 and st["indexed"] == 2 and st["index_model"] == HASH_MODEL and st["embedder"] is False
    assert mem.timeline()[0]["total"] == 2


def test_reindex_after_model_switch(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    obs, rep = ingest_path(str(ROOT / "samples" / "alarm_storm.zip"))
    kb.record(an.Analysis(obs, rep), "storm.zip")
    n = kb.stats()["lessons"]["pattern"]
    assert kb.embed_coverage() == {HASH_MODEL: n}
    kb.embedder = lambda texts: [[0.1] * 8 for _ in texts]; kb.embed_name = "fake-embed"
    mem = Memory(kb, live=None, interval_min=0)
    r = mem.reindex(background=False)
    assert r["remaining"] == 0 and r["done"] == n and kb.embed_coverage() == {"fake-embed": n}
    assert kb.search("alarm")                                    # rows scored with the new model vectors
    kb.embedder = lambda texts: (_ for _ in ()).throw(RuntimeError("down")); kb.embed_name = "down-embed"
    r = mem.reindex(background=False)                           # the model is down: nothing rewritten, error reported
    assert r["done"] == 0 and r["error"]
    kb.embedder = None; kb.embed_name = ""
    assert kb.reindex()["remaining"] == 0                       # back to the hash index: everything re-embedded locally


def test_live_learner_records_patterns(tmp_path):
    from watchover.live import LiveStore
    kb = Knowledge(str(tmp_path / "k.db"))
    obs, _ = ingest_path(str(ROOT / "samples" / "alarm_storm.zip"))

    class FakeLive:
        def snapshot(self, env=None):
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            base = obs[0].timestamp
            for o in obs:
                o.timestamp = now - timedelta(minutes=30) + (o.timestamp - base) / 10
            return obs
    mem = Memory(kb, FakeLive(), interval_min=0)
    out = mem.learn_now()
    assert out["events"] == len(obs) and out["lessons"] >= 1 and mem.stats()["last_learn"]["lessons"] == out["lessons"]
    assert isinstance(LiveStore, type)
