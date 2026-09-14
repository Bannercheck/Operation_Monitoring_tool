from pathlib import Path

from signal_sprint.analysis import Analysis
from signal_sprint.pipeline import ingest_path
from signal_sprint.playbook import Playbook, similarity

ROOT = Path(__file__).resolve().parents[1]


def test_playbook_records_lookup_notes(tmp_path):
    pb = Playbook(str(tmp_path / "pb.db"))
    obs, rep = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    a = Analysis(obs, rep)
    n = pb.record(a, "demo_mixed.zip")
    assert n >= 4 and pb.all()
    root = a.signal_by_id[a.incidents[0].root_cause_signal].template
    e = pb.get(root)
    assert e and e["root_cause_count"] == 1 and e["recoveries"] == {"restart": 1} and e["datasets"] == ["demo_mixed.zip"]
    assert "slow queries" in e["runbook"].lower()  # seeded from scenario.RECOMMENDATIONS ("duration")
    pb.record(a, "demo_mixed.zip")           # same dataset again -> no double count
    assert pb.get(root)["occurrences"] == 1
    pb.record(a, "other.zip")                 # another dataset -> counted
    e = pb.get(root)
    assert e["occurrences"] == 2 and e["recoveries"]["restart"] == 2 and e["datasets"] == ["demo_mixed.zip", "other.zip"]
    pb.set_notes(root, resolution="Connection pool exhausted; restart cleared it. Raised pool size.", runbook="- check pg_stat_activity\n- restart if > 90% pool")
    assert "pool" in pb.get(root)["resolution"] and root in [r["template"] for r in pb.all("pool")]
    fuzzy = pb.lookup("log: duration: <n>ms statement: select * from customers where id = <n>")
    assert fuzzy and fuzzy["template"] == root and fuzzy["similarity"] >= 0.6
    assert pb.lookup("completely unrelated thing happened") is None
    assert similarity("db timeout error", "db timeout warning") == 0.5
    pb.delete(root)
    assert pb.get(root) is None
