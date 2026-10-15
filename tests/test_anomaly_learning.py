"""Dynamic learning: per (host, signal) maturity from the incoming rollups and metric samples, continuous learning after it,
level-shift re-learning vs incidents, migration of existing hosts, and the fixed ('sabit') mode unchanged."""
from datetime import datetime, timedelta, timezone

import pytest

from watchover import anomaly as wo_an
from watchover.history import History
from watchover.knowledge import Knowledge

from test_api import client, token  # noqa: F401 - the API fixture (signed-in admin)

UTC = timezone.utc
MON = datetime(2025, 1, 6, tzinfo=UTC)                 # a Monday (in the past: anomalies' real-time last_seen is after it)


def _kb(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db"))
    History(kb)
    return kb


def _seed(kb, start, end, total=lambda t: 4, errors=lambda t: 1, host="web-01", env="prod"):
    rows, t = [], start
    while t < end:
        rows.append((t.strftime("%Y-%m-%dT%H:%M"), env, host, "agent-1", total(t), errors(t)))
        t += timedelta(minutes=1)
    kb.executemany("INSERT INTO slo_minute (minute, env, host, sender, total, errors) VALUES (?,?,?,?,?,?)", rows)


def _tr(kb, days=7, mode="dinamik", min_days=1):
    return wo_an.AnomalyTracker(kb, None, learning_days=days, learning_mode=mode, learning_min_days=min_days)


def _sig(tr, now, sig, host="web-01", env="prod"):
    return tr.learning(host, env, now, sig)


def test_graduates_by_coverage_and_stability(tmp_path):
    kb = _kb(tmp_path)
    _seed(kb, MON + timedelta(hours=10), MON + timedelta(days=2, hours=12))       # Mon 10:00 -> Wed 12:00, steady
    tr = _tr(kb)
    r = _sig(tr, MON + timedelta(hours=20), "rate")                                 # 10 hours in
    codes = [x["code"] for x in r["reasons"]]
    assert r["learning"] and "coverage" in codes and "stability_wait" in codes and "min" in codes and 0 < r["pct"] < 100
    r = _sig(tr, MON + timedelta(days=1, hours=12), "rate")                         # Tue 12:00: 24/24 hours seen, a day old, not yet compared
    assert r["learning"] and [x["code"] for x in r["reasons"]] == ["stability_wait"] and r["coverage"] == 24
    h = tr.learning("web-01", "prod", MON + timedelta(days=2, hours=2))              # Wed 02:00: Tuesday matched Monday -> learned
    assert h["learning"] is False and {s["signal"]: s["learning"] for s in h["signals"]} == {"rate": False, "errors": False}
    rate = [s for s in h["signals"] if s["signal"] == "rate"][0]
    assert rate["note"]["code"] == "learned" and rate["delta"] <= 15 and rate["weekly"] is None
    ov = tr.learning_overview(MON + timedelta(days=2, hours=2))
    assert ov["mode"] == "dinamik" and ov["learned"] == 1 and ov["learning"] == []


def test_weekly_series_waits_for_a_weekend_day(tmp_path):
    kb = _kb(tmp_path)
    busy = lambda t: 10 if 8 <= t.hour < 20 else 1                                  # people-driven: busy days, quiet nights
    _seed(kb, MON, MON + timedelta(days=6, hours=6), total=busy, errors=lambda t: 0)
    tr = _tr(kb)
    fri = _sig(tr, MON + timedelta(days=4, hours=2), "rate")
    assert fri["learning"] and fri["reasons"][0] == {"code": "weekly", "need": "weekend"} and fri["delta"] is not None and fri["delta"] <= 15
    assert _sig(tr, MON + timedelta(days=4, hours=2), "errors")["learning"] is False   # a flat series does not wait for the weekend
    h = tr.learning("web-01", "prod", MON + timedelta(days=4, hours=2))
    assert h["learning"] and any(s["signal"] == "rate" and s["learning"] for s in h["signals"])
    sun = _sig(tr, MON + timedelta(days=6, hours=2), "rate")                       # Saturday seen (same shape): learned before the cap
    assert sun["learning"] is False and sun["note"]["code"] == "learned" and sun["days"]["we"] >= 1 and sun["weekly"] is False


def test_quiet_series_stays_learning_until_the_upper_bound(tmp_path):
    kb = _kb(tmp_path)
    _seed(kb, MON, MON + timedelta(days=4), total=lambda t: 1 if t.minute == 0 else 0, errors=lambda t: 0)   # 1 event / hour
    tr = _tr(kb, days=3)
    r = _sig(tr, MON + timedelta(days=2), "rate")
    assert r["learning"] and r["reasons"][0]["code"] == "quiet" and r["pct"] < 100
    r = _sig(tr, MON + timedelta(days=3, hours=2), "rate")
    assert r["learning"] is False and r["note"]["code"] == "cap" and "quiet" in r["note"]["open"]
    assert _tr(kb, days=90)._learn_cfg()["max"] == wo_an.LEARN_CAP_DAYS                # the upper bound never exceeds 30 days


def _shifted(tmp_path, errors_up=False):
    kb = _kb(tmp_path)
    shift = MON + timedelta(days=3)                                                 # Thursday 00:00: a deployment triples the log volume
    _seed(kb, MON, shift)
    _seed(kb, shift, shift + timedelta(hours=32), total=lambda t: 12, errors=(lambda t: 6) if errors_up else (lambda t: 1))
    tr = _tr(kb)
    assert tr.learning("web-01", "prod", MON + timedelta(days=2, hours=2))["learning"] is False
    return kb, tr, shift


def test_level_shift_relearns_that_series_only(tmp_path):
    kb, tr, shift = _shifted(tmp_path)
    now = shift + timedelta(days=1, hours=6)                                        # 29 h at the new level, errors unchanged
    rate, errs = _sig(tr, now, "rate"), _sig(tr, now, "errors")
    assert rate["learning"] and rate["note"]["code"] == "shift" and rate["note"]["pct"] >= 150 and rate["started"] == shift.isoformat()
    assert errs["learning"] is False                                                # the other series keeps its baseline
    assert tr.learning("web-01", "prod", now)["learning"] is True                   # rate is a key signal
    ev = tr.learning_overview(now)["events"]
    assert ev and ev[0]["signal"] == "rate" and ev[0]["pct"] >= 150 and not ev[0]["manual"]


def test_level_shift_with_errors_stays_an_anomaly(tmp_path):
    kb, tr, shift = _shifted(tmp_path, errors_up=True)
    now = shift + timedelta(days=1, hours=6)
    rate, errs = _sig(tr, now, "rate"), _sig(tr, now, "errors")
    assert rate["learning"] is False and rate["shift"]["incident"] == "errors" and rate["shift"]["dir"] == 1
    assert errs["learning"] is False and errs["shift"]["incident"] == "errors"
    assert tr.learning_overview(now)["events"] == []


def test_level_shift_with_an_alert_stays_an_anomaly(tmp_path):
    kb, tr, shift = _shifted(tmp_path)
    tr._upsert("metric", "threshold:prod:web-01:cpu", env="prod", host="web-01", metric="cpu", title="cpu 97%", detail="",
               observed=97, baseline=85, spread=0, score=5, series=[97])            # an alert during the shift (open)
    rate = _sig(tr, shift + timedelta(days=1, hours=6), "rate")
    assert rate["learning"] is False and rate["shift"]["incident"] == "alert"


def test_existing_hosts_keep_their_graduation(tmp_path):
    kb = _kb(tmp_path)
    now = MON + timedelta(days=12)
    _seed(kb, now - timedelta(days=2), now, host="old-01")                          # graduated early by an operator (earlier version)
    _seed(kb, now - timedelta(days=10), now - timedelta(days=10) + timedelta(minutes=30), host="old-02")
    _seed(kb, now - timedelta(days=1), now, host="old-02")                          # learned by the fixed week (10 days of history)
    _seed(kb, now - timedelta(hours=10), now, host="mid-01")                        # was mid-way through its week
    tr = _tr(kb)                                                                    # creates the tables; the rows are an earlier version's
    kb.executemany("INSERT INTO anomaly_learning (host, env, started, graduated_at) VALUES (?,?,?,?)", [
        ("old-01", "prod", (now - timedelta(days=2)).isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat(timespec="seconds")),
        ("old-02", "prod", (now - timedelta(days=10)).isoformat(timespec="seconds"), ""),
        ("mid-01", "prod", (now - timedelta(hours=10)).isoformat(timespec="seconds"), "")])
    o1, o2, m1 = (tr.learning(h, "prod", now) for h in ("old-01", "old-02", "mid-01"))
    assert o1["learning"] is False and all(s["note"]["code"] == "kept" for s in o1["signals"])
    assert o2["learning"] is False and all(s["note"]["code"] == "history" for s in o2["signals"])
    assert m1["learning"] is True
    fixed = _tr(kb, mode="sabit")
    assert [fixed.learning(h, "prod", now)["learning"] for h in ("old-01", "old-02", "mid-01")] == [False, False, True]


def test_sabit_mode_is_the_fixed_week(tmp_path):
    kb = _kb(tmp_path)
    _seed(kb, MON, MON + timedelta(days=3))                                         # three steady days: dinamik would be done
    tr = _tr(kb, mode="sabit")
    now = MON + timedelta(days=3)
    r = tr.learning("web-01", "prod", now)
    assert r == {"host": "web-01", "env": "prod", "learning": True, "started": MON.isoformat(), "ends": (MON + timedelta(days=7)).isoformat(),
                 "pct": round(100 * 3 / 7, 1), "graduated_at": ""}
    assert tr.learning("web-01", "prod", now, "rate") == r                          # signals do not matter in sabit
    assert tr.learning("web-01", "prod", MON + timedelta(days=7))["learning"] is False
    ov = tr.learning_overview()
    assert ov["mode"] == "sabit" and ov["days"] == 7 and "events" not in ov
    assert kb._exec("SELECT COUNT(*) AS n FROM anomaly_signal")[0]["n"] == 0        # nothing of the dynamic store is touched
    assert _tr(kb).learning("web-01", "prod", now)["learning"] is False             # the same data, dinamik: learned


def test_operator_signal_actions_and_metrics(tmp_path):
    kb = _kb(tmp_path)
    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    _seed(kb, now - timedelta(hours=10), now)
    tr = _tr(kb)
    per_minute = [{"host": "web-01", "env": "prod", "metric": "cpu", "minute": _h - timedelta(minutes=m), "value": 30.0}
                  for _h in (now.replace(minute=0) - timedelta(hours=1), now.replace(minute=0)) for m in range(1, 61)]
    tr.observe_metrics(per_minute, now)
    cpu = _sig(tr, now, "cpu")
    assert cpu["signal"] == "cpu" and cpu["learning"] and cpu["coverage"] == 2
    h = tr.graduate("web-01", "prod", "rate")                                       # one signal only
    st = {s["signal"]: s for s in h["signals"]}
    assert st["rate"]["learning"] is False and st["rate"]["note"]["code"] == "operator" and st["errors"]["learning"] and h["learning"]
    h = tr.relearn("web-01", "prod", "rate")                                        # 'bu sinyali öğrenmeye geri al'
    st = {s["signal"]: s for s in h["signals"]}
    assert st["rate"]["learning"] and st["rate"]["note"]["code"] == "manual"
    assert tr.learning_overview()["events"][0]["manual"] is True
    with pytest.raises(KeyError):
        tr.relearn("web-01", "prod", "nosuch")
    assert tr.graduate("web-01", "prod")["learning"] is False                       # the host-wide action is kept
    assert tr.host_learning("web-01")["known"] and tr.host_learning("ghost")["known"] is False


def test_api_learning_endpoints(client):
    from watchover import settings
    h = token(client)
    svc = client.app.state.services
    now = datetime.now(UTC)
    _seed(svc.kb, now - timedelta(hours=5), now, host="api-01")
    svc.anomalies.reset_learning_cache()
    svc.anomalies.learning("api-01", "prod")
    r = client.get("/api/anomalies/learning", params={"host": "api-01"}, headers=h)
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["known"] and r["mode"] == "dinamik" and r["learning"] and {s["signal"] for s in r["signals"]} == {"rate", "errors"}
    assert client.get("/api/anomalies/learning", params={"host": "nobody"}, headers=h).json()["known"] is False
    assert client.post("/api/anomalies/learning/relearn", json={"host": "api-01", "env": "prod", "signal": "rate"}, headers=h).status_code == 200
    assert client.post("/api/anomalies/learning/graduate", json={"host": "api-01", "env": "prod", "signal": "errors"}, headers=h).status_code == 200
    st = client.get("/api/anomalies/stats", headers=h).json()["learning"]
    assert st["mode"] == "dinamik" and any(e["host"] == "api-01" and e["manual"] for e in st["events"])
    r = client.put("/api/anomalies/learning/settings", json={"mode": "sabit", "min_days": 2, "max_days": 5}, headers=h).json()
    assert r == {"mode": "sabit", "min_days": 2.0, "max_days": 5.0, "cap": 30} and settings.load()["anomaly_learning_mode"] == "sabit"
    assert "signals" not in client.get("/api/anomalies/learning", params={"host": "api-01", "env": "prod"}, headers=h).json()
    assert client.put("/api/anomalies/learning/settings", json={"mode": "x"}, headers=h).status_code == 400
    client.put("/api/anomalies/learning/settings", json={"mode": "dinamik", "min_days": 1, "max_days": 7}, headers=h)
    assert client.get("/api/system/settings", headers=h).json()["anomaly_learning_mode"] == "dinamik"
