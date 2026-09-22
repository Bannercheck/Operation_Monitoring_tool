"""scripts/loadtest.py helpers: corpus mix loads, freshening keeps years, batches parse through the live store with the detection cache."""
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import loadtest  # noqa: E402

from watchover.live import LiveStore  # noqa: E402


def test_templates_and_freshen_keep_the_year():
    t = loadtest.load_templates()
    assert len(t) >= 5 and any("ebs" in n or "ax_" in n or "nav" in n or "d365" in n for n, _, _ in t)
    rng = random.Random(1); now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    for _, lines, _ in t:
        for ln in lines[:20]:
            out = loadtest.freshen(ln, now, rng)
            assert "9996" not in out and ("2026" in out or "Sep 22" in out or "22/Sep/2026" in out or not any(ch.isdigit() for ch in ln[:4]))


def test_batches_ingest_and_detection_cache_warms():
    t = loadtest.load_templates(); rng = random.Random(2); store = LiveStore(maxlen=10_000)
    for i in range(30):
        send_as, lines, _ = t[i % len(t)]
        body = "\n".join(loadtest.freshen(rng.choice(lines), datetime.now(timezone.utc), rng) for _ in range(20)).encode()
        assert store.ingest(send_as, body, agent="load-001") > 0
    assert len(store._detect) == len(t) and all(v["n"] >= 1 and v["format"] for v in store._detect.values())
    cap = store.capacity()
    assert cap["ingest"]["batches"] == 30 and cap["ingest"]["avg_ms"] > 0 and cap["ring"]["span_s"] >= 0
