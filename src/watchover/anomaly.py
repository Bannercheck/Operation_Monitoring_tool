"""Anomaly tracking for operations: what deviates from its own baseline right now, and the operational steps taken on it.

Deterministic, no thresholds to tune: every key (host, service, metric) is compared with its own recent history using a robust
baseline (median and MAD of per-minute values), so a busy host and a quiet host are judged by their own normal.

    tracker = AnomalyTracker(kb, live)      # table `anomalies` in the shared database; live = LiveStore
    tracker.scan()                          # every minute from the background thread (start()), or by hand from the page
    tracker.list("open")                    # -> rows with steps (a per-kind checklist), owner, note, observed vs baseline, series
    tracker.set_status(id, "ack" | "resolved" | "ignored", owner=..., note=...)
    tracker.set_step(id, "verify", True)

Kinds:  errors   ERROR+ per 5 min on a host far above its baseline (from the slo_minute rollups: 24 h of history)
        rate     event volume on a host far above its baseline (traffic surge / log storm)
        silence  a host that normally logs went quiet (agent alive or not: the steps say what to check)
        pattern  a message template never seen before, repeating (needs a warm-up: something older than 1 h in the memory table)
        metric   cpu / memory / disk / gpu far above the host's own last hours (from the live metric series)
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from .models import SEV_RANK

UTC = timezone.utc
KINDS = ("errors", "rate", "silence", "pattern", "metric")
THRESHOLD_DEFAULTS = {"cpu": 85.0, "memory": 90.0, "disk": 90.0, "gpu": 95.0}


def threshold_settings() -> dict[str, float]:
    """Alert thresholds from System › Settings (thr_cpu …); the scenario's METRIC_THRESHOLDS and then the defaults fill the gaps."""
    out = dict(THRESHOLD_DEFAULTS)
    try:
        from . import scenario
        out.update({k: float(v) for k, v in getattr(scenario, "METRIC_THRESHOLDS", {}).items()})
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import settings
        cfg = settings.load()
        for k in THRESHOLD_DEFAULTS:
            v = cfg.get(f"thr_{k}")
            if v not in (None, ""):
                out[k] = float(v)
    except Exception:  # noqa: BLE001
        pass
    return out
STATUSES = ("open", "ack", "resolved", "ignored")
WINDOW_MIN = 5                                    # what "now" means
BASELINE_MIN = 24 * 60                            # how far back the baseline looks (rollups)
STEPS: dict[str, list[str]] = {                   # the operational checklist per kind (labels in i18n: an_step_<id>)
    "errors": ["verify", "scope", "playbook", "assign", "action", "close"],
    "rate": ["verify", "scope", "source", "assign", "close"],
    "silence": ["agent", "host", "network", "close"],
    "pattern": ["verify", "playbook", "assign", "action", "close"],
    "metric": ["verify", "top", "capacity", "assign", "close"],
}
MIN_EVENTS = {"errors": 10, "rate": 50, "pattern": 5}
Z_LIMIT = 4.0                                     # robust z-score above which a value is an anomaly
METRIC_DELTA = 15.0                               # ... and for metrics also at least this many points above the baseline


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def robust(values: list[float]) -> tuple[float, float]:
    """Median and scaled MAD (a standard deviation that ignores outliers)."""
    if not values:
        return 0.0, 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return float(med), float(1.4826 * mad)


def zscore(value: float, med: float, mad: float) -> float:
    spread = max(mad, 1.0, 0.1 * med)             # never divide by a flat baseline; small baselines need a real jump
    return (value - med) / spread


class AnomalyTracker:
    def __init__(self, kb, live=None, every: float = 60.0):
        self.kb, self.live, self.every = kb, live, every
        self.stop = threading.Event()
        self.thread = None
        self.runs = 0
        self.last_run = ""
        self.last_scan_ms = 0.0
        self.last_scan_events = 0
        self.last_found: list[dict] = []
        self.on_open = None                      # memory feed: called with the row of every newly opened anomaly
        kb._exec(f"""CREATE TABLE IF NOT EXISTS anomalies (id {kb.pk}, kind TEXT NOT NULL, key TEXT NOT NULL, env TEXT DEFAULT '', host TEXT DEFAULT '',
            service TEXT DEFAULT '', metric TEXT DEFAULT '', title TEXT DEFAULT '', detail TEXT DEFAULT '', observed REAL DEFAULT 0, baseline REAL DEFAULT 0,
            spread REAL DEFAULT 0, score REAL DEFAULT 0, hits INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, cleared_at TEXT DEFAULT '',
            status TEXT DEFAULT 'open', owner TEXT DEFAULT '', note TEXT DEFAULT '', steps TEXT DEFAULT '[]', series TEXT DEFAULT '[]',
            action_id INTEGER DEFAULT 0, updated_at TEXT, resolved_at TEXT DEFAULT '')""")
        kb._exec("CREATE INDEX IF NOT EXISTS anomalies_status ON anomalies(status, last_seen)")
        kb._exec("CREATE TABLE IF NOT EXISTS anomaly_templates (key TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, count INTEGER DEFAULT 0)")

    # ---------------------------------------------------------------- background
    def start(self) -> "AnomalyTracker":
        if self.thread and self.thread.is_alive():
            return self
        self.stop.clear()

        def loop():
            while not self.stop.wait(self.every):
                try:
                    self.scan()
                except Exception:  # noqa: BLE001 - never kill the loop
                    pass
        self.thread = threading.Thread(target=loop, daemon=True, name="anomaly-tracker")
        self.thread.start()
        return self

    def shutdown(self) -> None:
        self.stop.set()

    # ---------------------------------------------------------------- detection
    def scan(self, now: datetime | None = None) -> list[dict]:
        t0 = time.perf_counter()
        try:
            return self._scan(now)
        finally:
            self.last_scan_ms = round((time.perf_counter() - t0) * 1000, 1)

    def _window(self, since: datetime):
        """The live window, read once per scan however many phases ask for it (a database-served window is expensive)."""
        key = since.isoformat(timespec="seconds")
        memo = getattr(self, "_win", None)
        if memo is None or memo.get("key") != key:
            try:
                obs = self.live.snapshot(since=since)
            except TypeError:
                obs = self.live.snapshot()
            self._win = memo = {"key": key, "obs": obs}
        return memo["obs"]

    def _scan(self, now: datetime | None = None) -> list[dict]:
        """One pass over the live feed; returns the anomalies opened or refreshed by this pass."""
        now = now or datetime.now(UTC)
        self._win = None
        found: list[dict] = []
        if self.live is not None:
            found += self._scan_counts(now)
            found += self._scan_patterns(now)
            found += self._scan_metrics(now)
            found += self._scan_thresholds(now)
            self._mine_templates(now)
        self._clear_recovered({f["key"] for f in found})
        self.runs += 1
        self.last_run = _now()
        self.last_found = found
        return found

    def _rollup_baseline(self, host: str, env: str, now: datetime) -> dict:
        """Per-minute totals and errors of one host over the baseline window, from the slo_minute rollups (older than the current window)."""
        start = (now - timedelta(minutes=BASELINE_MIN)).strftime("%Y-%m-%dT%H:%M")
        cut = (now - timedelta(minutes=WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M")
        rows = self.kb._exec("SELECT minute, SUM(total) AS t, SUM(errors) AS e FROM slo_minute WHERE host=? AND env=? AND minute >= ? AND minute < ? GROUP BY minute",
                             (host, env, start, cut))
        minutes = max(1, min(BASELINE_MIN, int((now - datetime.fromisoformat(min(r["minute"] for r in rows) + ":00").replace(tzinfo=UTC)).total_seconds() // 60))) if rows else 0
        totals = [float(r["t"]) for r in rows] + [0.0] * max(0, minutes - len(rows))       # quiet minutes are real zeros
        errors = [float(r["e"]) for r in rows] + [0.0] * max(0, minutes - len(rows))
        return {"minutes": minutes, "total": totals, "errors": errors}

    def _scan_counts(self, now: datetime) -> list[dict]:
        out = []
        start = now - timedelta(minutes=WINDOW_MIN)
        obs = self._window(start)
        per_host: dict[tuple[str, str], list[int]] = {}
        services: dict[tuple[str, str], dict] = {}
        for o in obs:
            if o.timestamp < start:
                continue
            k = (o.host or "-", o.environment or "unknown")
            c = per_host.setdefault(k, [0, 0])
            c[0] += 1
            if SEV_RANK.get(o.severity, 0) >= 3:
                c[1] += 1
                if o.service:
                    services.setdefault(k, {})[o.service] = services.setdefault(k, {}).get(o.service, 0) + 1
        seen_hosts = set(per_host)
        for (host, env), (total, errors) in per_host.items():
            base = self._rollup_baseline(host, env, now)
            if base["minutes"] < 30:                                   # not enough history to say what normal is
                continue
            for kind, value, series in (("errors", errors, base["errors"]), ("rate", total, base["total"])):
                med, mad = robust(series)
                med5, mad5 = med * WINDOW_MIN, mad * (WINDOW_MIN ** 0.5)
                z = zscore(value, med5, mad5)
                if value >= MIN_EVENTS[kind] and z >= Z_LIMIT and value >= 2 * med5:
                    top = sorted(services.get((host, env), {}).items(), key=lambda kv: -kv[1])[:3]
                    detail = (f"{value:.0f} {'ERROR+' if kind == 'errors' else 'events'} in {WINDOW_MIN} min; normal for this host is {med5:.0f} ± {mad5:.0f}"
                              + (f"; top services: " + ", ".join(f"{s} ({n})" for s, n in top) if top else ""))
                    out.append(self._upsert(kind, f"{kind}:{env}:{host}", env=env, host=host, service=top[0][0] if top else "",
                                            title=f"{host}: {value:.0f} {'ERROR+' if kind == 'errors' else 'events'} / {WINDOW_MIN} min (normal {med5:.0f})",
                                            detail=detail, observed=value, baseline=med5, spread=mad5, score=z, series=series[-60:] + [value / WINDOW_MIN]))
        # silence: hosts that logged steadily over the baseline window but sent nothing in this one
        start_iso = (now - timedelta(minutes=BASELINE_MIN)).strftime("%Y-%m-%dT%H:%M")
        cut_iso = (now - timedelta(minutes=WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M")
        rows = self.kb._exec("SELECT host, env, COUNT(*) AS m, SUM(total) AS t FROM slo_minute WHERE minute >= ? AND minute < ? GROUP BY host, env", (start_iso, cut_iso))
        for r in rows:
            host, env = r["host"], r["env"]
            if (host, env) in seen_hosts or host in ("-", "") or int(r["m"]) < BASELINE_MIN * 0.5:
                continue
            last = self.kb._exec("SELECT MAX(minute) AS m FROM slo_minute WHERE host=? AND env=?", (host, env))[0]["m"] or ""
            quiet = max(0.0, (now - datetime.fromisoformat(last + ":00").replace(tzinfo=UTC)).total_seconds() / 60) if last else float(WINDOW_MIN)
            if quiet < WINDOW_MIN * 2:
                continue
            per_min = float(r["t"]) / max(int(r["m"]), 1)
            out.append(self._upsert("silence", f"silence:{env}:{host}", env=env, host=host, title=f"{host}: silent for {quiet:.0f} min (normally {per_min:.1f} events / min)",
                                    detail=f"No events since {last.replace('T', ' ')}; the host logged in {int(r['m'])} of the last {BASELINE_MIN} minutes",
                                    observed=quiet, baseline=0.0, spread=0.0, score=min(quiet / WINDOW_MIN, 10.0), series=[]))
        return out

    def _scan_patterns(self, now: datetime) -> list[dict]:
        from .analysis import template_of
        start = now - timedelta(minutes=WINDOW_MIN)
        obs = self._window(start)
        counts: dict[str, dict] = {}
        memo: dict[str, str] = {}                      # live traffic repeats messages heavily: template each distinct text once per scan
        for o in obs:
            if o.timestamp < start or SEV_RANK.get(o.severity, 0) < 2:
                continue
            msg = o.message or ""
            tpl = o.template or memo.get(msg)
            if tpl is None:
                tpl = template_of(msg)
                if len(memo) < 100_000:
                    memo[msg] = tpl
            if not tpl:
                continue
            c = counts.setdefault(tpl, {"n": 0, "host": o.host or "", "env": o.environment or "unknown", "service": o.service or "", "sev": o.severity})
            c["n"] += 1
        if not counts:
            return []
        oldest = self.kb._exec("SELECT MIN(first_seen) AS f FROM anomaly_templates")[0]["f"]
        warm = bool(oldest) and datetime.fromisoformat(oldest) <= now - timedelta(hours=1)
        known = {r["key"] for r in self.kb._exec("SELECT key FROM anomaly_templates")}
        out = []
        ts = now.isoformat(timespec="seconds")
        for tpl, c in counts.items():
            if tpl in known:
                self.kb._exec("UPDATE anomaly_templates SET last_seen=?, count=count+? WHERE key=?", (ts, c["n"], tpl))
                continue
            self.kb._exec("INSERT INTO anomaly_templates (key, first_seen, last_seen, count) VALUES (?,?,?,?)", (tpl, ts, ts, c["n"]))
            if warm and c["n"] >= MIN_EVENTS["pattern"]:
                out.append(self._upsert("pattern", f"pattern:{tpl[:160]}", env=c["env"], host=c["host"], service=c["service"], metric=c["sev"],   # metric column carries the severity
                                        title=f"New {c['sev']} pattern × {c['n']}: {tpl[:90]}",
                                        detail=f"First seen {ts.replace('T', ' ')} on {c['host'] or '-'} / {c['service'] or '-'}; never seen in the last days",
                                        observed=c["n"], baseline=0.0, spread=0.0, score=min(c["n"] / MIN_EVENTS["pattern"] * 2, 10.0), series=[]))
        return out

    def _scan_metrics(self, now: datetime) -> list[dict]:
        ms = self.live.metric_stats(180) if hasattr(self.live, "metric_stats") else {}
        per: dict[tuple[str, str], list[tuple]] = {}
        for r in ms.get("per_minute", []):
            per.setdefault((r["host"], r["metric"]), []).append((r["minute"], float(r["value"]), r.get("env", "unknown")))
        out = []
        cut = now - timedelta(minutes=WINDOW_MIN)
        for (host, metric), rows in per.items():
            hist = [v for m, v, _ in rows if m < cut]
            cur = [v for m, v, _ in rows if m >= cut]
            if len(hist) < 30 or not cur:
                continue
            value = statistics.fmean(cur)
            med, mad = robust(hist)
            z = zscore(value, med, mad)
            if z >= Z_LIMIT and value - med >= METRIC_DELTA:
                env = rows[-1][2]
                out.append(self._upsert("metric", f"metric:{env}:{host}:{metric}", env=env, host=host, metric=metric,
                                        title=f"{host}: {metric} {value:.0f}% (last hours {med:.0f}%)",
                                        detail=f"{metric} averaged {value:.0f}% over the last {WINDOW_MIN} min; this host's normal is {med:.0f} ± {mad:.0f}%",
                                        observed=value, baseline=med, spread=mad, score=z, series=hist[-60:] + [value]))
        return out

    def _mine_templates(self, now: datetime) -> None:
        """Drain learns from the last window of the live feed (messages newer than the previous pass)."""
        store = getattr(self, "templates", None)
        if store is None:
            return
        since = getattr(self, "_mined_until", None) or (now - timedelta(minutes=WINDOW_MIN))
        obs = self._window(since)
        try:
            store.learn(obs[:20000], source="live")
        except Exception:  # noqa: BLE001
            return
        self._mined_until = now

    def _scan_thresholds(self, now: datetime) -> list[dict]:
        """Hard limits next to the baseline logic: a host whose latest CPU / memory / disk / GPU reading is at or above the configured
        threshold gets a 'metric' anomaly right away (no history needed), so the bell, the page and the alert rules all see it."""
        ms = self.live.metric_stats(5) if hasattr(self.live, "metric_stats") else {}
        thr = threshold_settings()
        latest: dict[tuple[str, str], tuple] = {}
        for r in ms.get("per_minute", []):
            k = (r["host"], r["metric"])
            if k not in latest or r["minute"] > latest[k][0]:
                latest[k] = (r["minute"], float(r["value"]), r.get("env", "unknown"))
        out = []
        for (host, metric), (_m, value, env) in latest.items():
            limit = thr.get(metric)
            if limit is None or value < limit:
                continue
            out.append(self._upsert("metric", f"threshold:{env}:{host}:{metric}", env=env, host=host, metric=metric,
                                    title=f"{host}: {metric} {value:.0f}% ≥ {limit:.0f}%",
                                    detail=f"{metric} is at {value:.0f}% on {host} ({env}); the alert threshold is {limit:.0f}% (System › Settings)",
                                    observed=value, baseline=float(limit), spread=0.0, score=round((value - limit) / max(1.0, 100 - limit) * 5 + 3, 2), series=[value]))
        return out

    # ---------------------------------------------------------------- store
    def _upsert(self, kind: str, key: str, *, env: str, host: str, title: str, detail: str, observed: float, baseline: float, spread: float,
                score: float, series: list, service: str = "", metric: str = "") -> dict:
        now = _now()
        rows = self.kb._exec("SELECT id, hits FROM anomalies WHERE key=? AND status IN ('open', 'ack') ORDER BY id DESC LIMIT 1", (key,))
        ser = json.dumps([round(float(v), 2) for v in series][-61:])
        if rows:
            self.kb._exec("UPDATE anomalies SET title=?, detail=?, observed=?, baseline=?, spread=?, score=?, hits=hits+1, last_seen=?, cleared_at='', series=?, updated_at=? WHERE id=?",
                          (title, detail, float(observed), float(baseline), float(spread), round(float(score), 2), now, ser, now, rows[0]["id"]))
            return self.get(rows[0]["id"])
        steps = json.dumps([{"id": s, "done": False, "ts": ""} for s in STEPS[kind]])
        aid = self.kb._insert("INSERT INTO anomalies (kind, key, env, host, service, metric, title, detail, observed, baseline, spread, score, hits, first_seen, last_seen, status, steps, series, updated_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,'open',?,?,?)",
                              (kind, key, env, host, service, metric, title, detail, float(observed), float(baseline), float(spread), round(float(score), 2), now, now, steps, ser, now))
        return self.get(aid)

    def _clear_recovered(self, active_keys: set[str]) -> None:
        """Open anomalies whose key no longer trips are marked recovered (cleared_at) but stay open until an operator closes them."""
        for r in self.kb._exec("SELECT id, key, cleared_at FROM anomalies WHERE status IN ('open', 'ack')"):
            if r["key"] not in active_keys and not r["cleared_at"]:
                self.kb._exec("UPDATE anomalies SET cleared_at=? WHERE id=?", (_now(), r["id"]))

    @staticmethod
    def _row(r: dict) -> dict:
        d = dict(r)
        d["steps"] = json.loads(d.get("steps") or "[]")
        d["series"] = json.loads(d.get("series") or "[]")
        d["done"] = sum(1 for s in d["steps"] if s["done"])
        return d

    def get(self, aid: int) -> dict:
        rows = self.kb._exec("SELECT * FROM anomalies WHERE id=?", (aid,))
        if not rows:
            raise KeyError(aid)
        return self._row(rows[0])

    def list(self, status: str | None = None, env: str | None = None, kind: str | None = None, limit: int = 200) -> list[dict]:
        cond, params = [], []
        if status == "active":
            cond.append("status IN ('open', 'ack')")
        elif status:
            cond.append("status=?"); params.append(status)
        if env:
            cond.append("env=?"); params.append(env)
        if kind:
            cond.append("kind=?"); params.append(kind)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        return [self._row(r) for r in self.kb._exec(f"SELECT * FROM anomalies{where} ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'ack' THEN 1 ELSE 2 END, score DESC, last_seen DESC LIMIT ?", (*params, limit))]

    def set_status(self, aid: int, status: str, owner: str | None = None, note: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError("bad status")
        sets, params = ["status=?", "updated_at=?"], [status, _now()]
        if owner is not None:
            sets.append("owner=?"); params.append(owner.strip()[:80])
        if note is not None:
            sets.append("note=?"); params.append(note.strip()[:2000])
        if status in ("resolved", "ignored"):
            sets.append("resolved_at=?"); params.append(_now())
        self.kb._exec(f"UPDATE anomalies SET {', '.join(sets)} WHERE id=?", (*params, aid))
        return self.get(aid)

    def set_step(self, aid: int, step: str, done: bool) -> dict:
        a = self.get(aid)
        for s in a["steps"]:
            if s["id"] == step:
                s["done"], s["ts"] = bool(done), (_now() if done else "")
        status = a["status"]
        if done and status == "open":
            status = "ack"                                          # the first step taken acknowledges the anomaly
        self.kb._exec("UPDATE anomalies SET steps=?, status=?, updated_at=? WHERE id=?", (json.dumps(a["steps"]), status, _now(), aid))
        return self.get(aid)

    def attach_action(self, aid: int, action_id: int) -> dict:
        self.kb._exec("UPDATE anomalies SET action_id=?, updated_at=? WHERE id=?", (int(action_id), _now(), aid))
        return self.set_step(aid, "action", True)

    def delete(self, aid: int) -> None:
        self.kb._exec("DELETE FROM anomalies WHERE id=?", (aid,))

    def stats(self) -> dict:
        rows = self.kb._exec("SELECT status, kind, COUNT(*) AS n FROM anomalies GROUP BY status, kind")
        out = {"open": 0, "ack": 0, "resolved": 0, "ignored": 0, "kinds": {}, "runs": self.runs, "last_run": self.last_run, "last_scan_ms": self.last_scan_ms}
        for r in rows:
            out[r["status"]] = out.get(r["status"], 0) + int(r["n"])
            if r["status"] in ("open", "ack"):
                out["kinds"][r["kind"]] = out["kinds"].get(r["kind"], 0) + int(r["n"])
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        out["resolved_today"] = int(self.kb._exec("SELECT COUNT(*) AS n FROM anomalies WHERE status='resolved' AND resolved_at >= ?", (today,))[0]["n"])
        return out
