"""Anomaly tracking: baselines from the rollups, five anomaly kinds, dedup on repeat, operational steps, status flow, alert rule."""
from datetime import datetime, timedelta, timezone

from watchover import anomaly as wo_an
from watchover import notify as wo_notify
from watchover.history import History
from watchover.knowledge import Knowledge
from watchover.live import LiveStore
from watchover.models import Observation

UTC = timezone.utc
from pathlib import Path
ROOT_APP = Path(__file__).resolve().parents[1] / "app.py"


def _obs(ts, msg, sev="ERROR", host="web-01", service="api", env="prod", attrs=None):
    return Observation(timestamp=ts, message=msg, severity=sev, host=host, service=service, environment=env, attributes=attrs or {})


def _seed_rollups(kb, host="web-01", env="prod", minutes=600, per_min=4, err_per_min=1, until=None):
    """A steady host: `minutes` of history at per_min events / err_per_min errors, ending WINDOW_MIN before now."""
    until = until or datetime.now(UTC)
    for i in range(wo_an.WINDOW_MIN, wo_an.WINDOW_MIN + minutes):
        m = (until - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M")
        kb._exec("INSERT INTO slo_minute (minute, env, host, sender, total, errors) VALUES (?,?,?,?,?,?)", (m, env, host, "agent-1", per_min, err_per_min))


def test_robust_baseline_and_zscore():
    med, mad = wo_an.robust([3, 4, 5, 4, 6, 4, 60])
    assert med == 4 and 0 < mad < 2 and wo_an.zscore(60, med, mad) > wo_an.Z_LIMIT and wo_an.zscore(5, med, mad) < 2
    assert wo_an.robust([]) == (0.0, 0.0) and wo_an.zscore(3, 0, 0) == 3


def test_error_storm_detected_once_with_steps(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db")); History(kb); ls = LiveStore()
    _seed_rollups(kb)
    now = datetime.now(UTC)
    ls.buf.extend(_obs(now - timedelta(seconds=5 * i), f"db timeout for order {i}", "ERROR") for i in range(40))   # 40 errors in 5 min, normal is ~5
    tr = wo_an.AnomalyTracker(kb, ls)
    found = tr.scan(now)
    kinds = {f["kind"] for f in found}
    assert "errors" in kinds                                                # 40 errors in 5 min against a normal of 5
    a = [f for f in found if f["kind"] == "errors"][0]
    assert a["host"] == "web-01" and a["observed"] == 40 and a["baseline"] == 5 and a["score"] >= wo_an.Z_LIMIT and a["status"] == "open"
    assert [s["id"] for s in a["steps"]] == wo_an.STEPS["errors"] and a["done"] == 0 and len(a["series"]) > 30
    again = tr.scan(now + timedelta(seconds=30))
    assert [f["id"] for f in again if f["kind"] == "errors"] == [a["id"]] and tr.get(a["id"])["hits"] == 2     # same key: refreshed, not duplicated
    assert tr.stats()["open"] == 1 and tr.stats()["kinds"] == {"errors": 1}
    # operational steps: first step acknowledges, action attaches, resolve closes
    b = tr.set_step(a["id"], "verify", True)
    assert b["status"] == "ack" and b["done"] == 1 and b["steps"][0]["ts"]
    b = tr.attach_action(a["id"], 7)
    assert b["action_id"] == 7 and any(s["id"] == "action" and s["done"] for s in b["steps"])
    b = tr.set_status(a["id"], "resolved", owner="tayfur", note="pool raised")
    assert b["status"] == "resolved" and b["owner"] == "tayfur" and b["resolved_at"] and tr.stats()["resolved_today"] == 1
    assert tr.list("active") == [] and tr.list("resolved")[0]["id"] == a["id"]
    # recovered but still open anomalies are flagged, and a quiet feed opens nothing
    ls.buf.extend(_obs(now - timedelta(seconds=i), f"cache miss {i}", "ERROR", host="web-02") for i in range(40))
    _seed_rollups(kb, host="web-02")
    c = [f for f in tr.scan(now) if f["host"] == "web-02"][0]
    ls.clear()
    assert tr.scan(now + timedelta(minutes=10)) == [] and tr.get(c["id"])["cleared_at"] and tr.get(c["id"])["status"] == "open"


def test_silence_pattern_and_metric(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db")); History(kb); ls = LiveStore()
    now = datetime.now(UTC)
    _seed_rollups(kb, host="db-01", minutes=1200, until=now - timedelta(minutes=30))        # steady host that stopped 35 min ago
    tr = wo_an.AnomalyTracker(kb, ls)
    s = [f for f in tr.scan(now) if f["kind"] == "silence"]
    assert len(s) == 1 and s[0]["host"] == "db-01" and s[0]["observed"] >= 30 and [x["id"] for x in s[0]["steps"]] == wo_an.STEPS["silence"]
    # patterns: the first pass only learns (warm-up); a template first seen after that, repeating, is an anomaly
    ls.buf.extend(_obs(now - timedelta(seconds=i), f"user {i} logged in", "WARN") for i in range(6))
    assert not [f for f in tr.scan(now) if f["kind"] == "pattern"]
    kb._exec("UPDATE anomaly_templates SET first_seen=?", ((now - timedelta(hours=2)).isoformat(timespec="seconds"),))
    ls.buf.extend(_obs(now - timedelta(seconds=i), f"disk /dev/sd{i} read-only remount", "CRITICAL", host="db-01") for i in range(6))
    p = [f for f in tr.scan(now) if f["kind"] == "pattern"]
    assert len(p) == 1 and "read-only remount" in p[0]["title"] and p[0]["observed"] == 6 and p[0]["host"] == "db-01"
    assert not [f for f in tr.scan(now) if f["kind"] == "pattern"]                                  # now known
    # metrics: 3 hours of cpu around 30 %, the last 5 minutes at 92 %
    for i in range(180, 0, -1):
        v = 92.0 if i <= wo_an.WINDOW_MIN else 30.0 + (i % 5)
        ls.metrics.append((now - timedelta(minutes=i) + timedelta(seconds=30), "web-07", "cpu", v, "prod"))
    m = [f for f in tr.scan(now) if f["kind"] == "metric"]
    assert len(m) == 1 and m[0]["metric"] == "cpu" and m[0]["observed"] == 92 and 30 <= m[0]["baseline"] <= 35 and m[0]["key"] == "metric:prod:web-07:cpu"


def test_alert_rule_on_new_anomalies(tmp_path, monkeypatch):
    kb = Knowledge(str(tmp_path / "k.db")); History(kb); ls = LiveStore()
    _seed_rollups(kb)
    now = datetime.now(UTC)
    ls.buf.extend(_obs(now - timedelta(seconds=5 * i), f"timeout {i}") for i in range(40))
    tr = wo_an.AnomalyTracker(kb, ls); tr.scan(now)
    n = wo_notify.Notifier(kb, {})
    n.add_rule("anomalies", "anomaly", 0, "", "P2", "email", "ops", 30)
    sent = []
    monkeypatch.setattr(n, "dispatch", lambda rule, key, title, body, force=False: sent.append((key, title)) or {"ok": 1, "title": title})
    eng = wo_notify.AlertEngine(n, ls); eng.anomalies = tr
    out = eng.evaluate()
    assert len(out) == 1 and sent[0][0].startswith("anomaly:errors:prod:web-01") and "Anomaly" in sent[0][1]
    tr.scan(now + timedelta(seconds=30))                    # hits == 2 -> not announced again
    assert eng.evaluate() == []


def test_ops_page_shows_anomaly_section(tmp_path, monkeypatch):
    """The Operations page renders the tracker with an anomaly seeded in the shared database; the step checkbox and buttons appear."""
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db")); monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home")); monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1")
    monkeypatch.setenv("LIVE_SPOOL", str(tmp_path / "live.jsonl")); monkeypatch.setenv("LIVE_PORT", "18696"); monkeypatch.delenv("DATABASE_URL", raising=False)
    kb = Knowledge(str(tmp_path / "k.db")); History(kb)
    tr = wo_an.AnomalyTracker(kb)
    a = tr._upsert("errors", "errors:prod:web-09", env="prod", host="web-09", title="web-09: 80 ERROR+ / 5 min (normal 6)", detail="seeded", observed=80, baseline=6, spread=2, score=9.5, series=[1, 2, 1, 16])
    kb.close()
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT_APP), default_timeout=90).run()
    assert not at.exception
    at.sidebar.radio(key="page").set_value("ops").run()
    assert not at.exception
    assert any("web-09" in m.value and "80 ERROR+" in m.value for m in at.markdown)   # TR or EN title
    assert any(c.key == f"an-{a['id']}-verify" for c in at.checkbox) and any(b.key == f"an-act-{a['id']}" for b in at.button)
    at.checkbox(key=f"an-{a['id']}-verify").check().run()
    assert not at.exception
    kb2 = Knowledge(str(tmp_path / "k.db"))
    row = wo_an.AnomalyTracker(kb2).get(a["id"])
    assert row["status"] == "ack" and row["done"] == 1
