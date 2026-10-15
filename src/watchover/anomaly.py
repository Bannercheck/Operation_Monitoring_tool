"""Anomaly tracking for operations: what deviates from its own baseline right now, and the operational steps taken on it.

Deterministic, no thresholds to tune: every key (host, service, metric) is compared with its own recent history using a robust
baseline (median and MAD of per-minute values), so a busy host and a quiet host are judged by their own normal.

    tracker = AnomalyTracker(kb, live)      # table `anomalies` in the shared database; live = LiveStore
    tracker.scan()                          # every minute from the background thread (start()), or by hand from the page
    tracker.list("open")                    # -> rows with steps (a per-kind checklist), owner, note, observed vs baseline, series
    tracker.set_status(id, "ack" | "resolved" | "ignored", owner=..., note=...)
    tracker.set_step(id, "verify", True)

Learning mode (`anomaly_learning_mode`, System › Settings):
  dinamik (default)  every (host, signal) series (log volume `rate`, `errors` per minute, each metric: cpu / memory / disk / gpu)
        learns from the incoming rollups and metric samples, one closed hour at a time, into an hour-of-day profile (weekday and
        weekend apart). A series graduates when it is at least `anomaly_learning_min_days` (1) old, 22 of the 24 hours of the day
        were observed, the learned profile changed less than 15 % from one day to the next, and - when it has a clear day/night
        cycle (people-driven) - it has seen at least one weekday and one weekend day. Quiet or rarely-seen series stay learning
        and say why; at `anomaly_learning_days` (7, at most 30) every series graduates. A host is learning while any of its key
        signals (rate, errors, cpu, memory) is. After graduation the profile keeps learning with decay (10 % per day); a level
        shift that lasts 24 h and does not coincide with errors / alerts sends that one series back to learning, with a note.
  sabit  the earlier behaviour: every host spends `anomaly_learning_days` from its first minute in the rollups.
What the statistical detectors find while a series learns is kept as a `learning` record (visible, never notified, never in the
bell); after it, errors / rate are judged against the same hour of the same weekday over the past weeks as well as the last 24 h,
so a Monday-morning surge or a nightly batch is normal, not an incident. Hard thresholds (CPU / memory / disk / GPU limits) and
the operations views never wait: they act from the first minute. An operator can end a host's (or one signal's) learning early,
or send one signal back to learning.

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
STATUSES = ("open", "ack", "resolved", "ignored")                # plus "learning": found while the host was still learning (never notified)
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

# dynamic learning (anomaly_learning_mode = dinamik): what makes a (host, signal) series mature
LEARN_MODES = ("dinamik", "sabit")
LEARN_CAP_DAYS = 30.0                             # anomaly_learning_days is the upper bound, never more than this
KEY_SIGNALS = ("rate", "errors", "cpu", "memory")  # a host is learning while any of these (that it has) is
SLO_SIGNALS = ("rate", "errors")                  # per-minute means from the slo_minute rollups; the rest come from metric samples
SIGNAL_FLOOR = {"rate": 1.0, "errors": 0.5}       # smallest level a relative change is measured against (metrics: METRIC_FLOOR points)
METRIC_FLOOR = 5.0
COVER_MIN = 22                                    # hours of the day (of 24) that must have been observed at least once
DAY_MIN_HOURS = 12                                # observed hours that make a day count (weekday / weekend, stability)
STABLE_MAX = 0.15                                 # day-over-day change of the learned profile below which a series is stable
CYCLE_AMP = 0.5                                   # daily peak-to-trough / mean above which a series follows people: waits for a weekend day
WEEKLY_DIFF = 0.25                                # weekend vs weekday level difference that proves a weekly cycle
QUIET_PER_MIN = 0.05                              # rate: fewer events / min than this on average is too quiet to learn
METRIC_MIN_SAMPLES = 10                           # minutes with a sample that make a metric hour observed
DECAY = 0.1                                       # after graduation: weight of each new day in the hour-of-day profile
SHIFT_REL = 0.5                                   # relative distance from the profile that counts as a shifted hour
SHIFT_HOURS = 24                                  # a shift this long, not an incident, sends the series back to learning
SEED_DAYS = 7                                     # history read for a series that is born learned (long-known or graduated host)
DIGEST_LAG_MIN = 5                                # an hour is read once it is this long closed (late rollup flushes)
_SIG_ORDER = ("rate", "errors", "cpu", "memory", "disk", "gpu")


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _hk(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def _hdt(hk: str) -> datetime:
    return datetime(int(hk[0:4]), int(hk[5:7]), int(hk[8:10]), int(hk[11:13]), tzinfo=UTC)


def _age(started: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(started)).total_seconds() / 86400


def _floor(sig: str) -> float:
    return SIGNAL_FLOOR.get(sig, METRIC_FLOOR)


def blank_state() -> dict:
    """What a series has learned: hour-of-day profiles (weekday p / weekend pw, n observations each), observed hours per hour of the
    day, the day being accumulated, weekday / weekend days seen, the last day-over-day change, the last 48 observed hours, a shift."""
    return {"hours": [0] * 24, "p": [None] * 24, "n": [0] * 24, "pw": [None] * 24, "nw": [0] * 24, "snap": None, "snapw": None,
            "day": "", "dayh": 0, "wd": 0, "we": 0, "delta": None, "obs": 0, "sum": 0.0, "last": "", "seen": "", "recent": [], "shift": None}


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _close_day(st: dict, floor: float) -> None:
    """End of a day: count it (weekday / weekend) and measure how much that day-type's profile moved since the previous one."""
    if not st["day"]:
        return
    we = datetime.strptime(st["day"], "%Y-%m-%d").weekday() >= 5
    prof, sk = ("pw", "snapw") if we else ("p", "snap")
    if st["dayh"] >= DAY_MIN_HOURS:
        st["we" if we else "wd"] += 1
        pairs = [(c, s) for c, s in zip(st[prof], st[sk] or []) if c is not None and s is not None]
        if len(pairs) >= DAY_MIN_HOURS:
            scale = sum(max(abs(s), floor) for _, s in pairs) / len(pairs)
            st["delta"] = round(sum(abs(c - s) for c, s in pairs) / len(pairs) / scale, 4)
    st[sk] = list(st[prof])


def _track_shift(st: dict, hk: str, value: float, h: int, we: bool, floor: float) -> bool:
    """A learned series: is this hour far from its profile? Consecutive far hours in one direction make a shift (3 normal hours end it)."""
    a, b = ("pw", "p") if we else ("p", "pw")
    e = st[a][h] if st[a][h] is not None else st[b][h]
    if e is None or st["obs"] < 24:
        return False
    rel = (value - e) / max(abs(e), floor)
    sh = st.get("shift")
    if abs(rel) >= SHIFT_REL:
        d = 1 if rel > 0 else -1
        if sh and sh["dir"] == d:
            sh["hours"] += 1
            sh["miss"] = 0
            sh["v"], sh["e"] = round(sh["v"] + value, 4), round(sh["e"] + e, 4)
        else:
            st["shift"] = {"dir": d, "since": hk, "hours": 1, "miss": 0, "v": round(value, 4), "e": round(e, 4), "incident": ""}
        return True
    if sh:
        sh["miss"] += 1
        if sh["miss"] >= 3:
            st["shift"] = None
    return False


def fold(st: dict, hk: str, value: float, learned: bool, floor: float) -> None:
    """Add one observed hour to a series. Learning: cumulative mean per hour of day; learned: decayed mean, and shifted hours do not
    move the profile (a level change is judged, not absorbed)."""
    dt = _hdt(hk)
    h, we, date = dt.hour, dt.weekday() >= 5, hk[:10]
    if st["day"] != date:
        _close_day(st, floor)
        st["day"], st["dayh"] = date, 0
    if not (learned and _track_shift(st, hk, value, h, we, floor)):
        prof, cnt = ("pw", "nw") if we else ("p", "n")
        k = st[cnt][h] + 1
        st[cnt][h] = k
        old = st[prof][h]
        a = max(DECAY, 1.0 / k) if learned else 1.0 / k
        st[prof][h] = round(value if old is None else old + a * (value - old), 4)
    st["hours"][h] += 1
    st["dayh"] += 1
    st["obs"] += 1
    st["sum"] = round(st["sum"] + value, 4)
    st["recent"] = (st["recent"] + [[hk, round(value, 4)]])[-48:]
    st["last"] = hk


def _cycle(st: dict, floor: float) -> float:
    """Daily peak-to-trough relative to the mean (0 for a flat series; needs 18 hours of the day)."""
    prof = st["p"] if sum(1 for v in st["p"] if v is not None) >= 18 else st["pw"]
    vals = [v for v in prof if v is not None]
    if len(vals) < 18:
        return 0.0
    return (max(vals) - min(vals)) / max(sum(vals) / len(vals), floor)


def _weekly(st: dict, floor: float) -> bool | None:
    """True / False once a weekday and a weekend day were seen (weekend level differs by WEEKLY_DIFF); None before."""
    if not (st["wd"] and st["we"]):
        return None
    a, b = _mean(st["p"]), _mean(st["pw"])
    if a is None or b is None:
        return None
    return abs(b - a) / max(a, floor) >= WEEKLY_DIFF


def _reasons(st: dict, sig: str, age: float, min_d: float) -> list[dict]:
    """Why a series is still learning, most telling first (empty: mature)."""
    floor = _floor(sig)
    out = []
    if sig == "rate" and st["obs"] >= 6 and st["sum"] / st["obs"] < QUIET_PER_MIN:
        out.append({"code": "quiet", "per_h": round(60 * st["sum"] / st["obs"], 1)})
    if _weekly(st, floor) is None and _cycle(st, floor) >= CYCLE_AMP:
        out.append({"code": "weekly", "need": "weekday" if st["we"] else "weekend"})
    cov = sum(1 for x in st["hours"] if x)
    if cov < COVER_MIN:
        out.append({"code": "coverage", "have": cov, "need": COVER_MIN})
    d = st["delta"]
    if d is not None and d > STABLE_MAX:
        out.append({"code": "unstable", "pct": round(d * 100)})
    elif d is None:
        out.append({"code": "stability_wait"})
    if age < min_d:
        out.append({"code": "min", "h": max(1, int(round((min_d - age) * 24)))})
    return out


def _progress(st: dict, age: float, min_d: float, max_d: float, reasons: list[dict]) -> float:
    codes = {r["code"] for r in reasons}
    d = st["delta"]
    parts = [min(1.0, age / min_d) if min_d > 0 else 1.0, min(1.0, sum(1 for x in st["hours"] if x) / COVER_MIN),
             1.0 if d is not None and d <= STABLE_MAX else (0.9 * STABLE_MAX / d if d else 0.0)]
    if "weekly" in codes:
        parts.append(0.5 if (st["wd"] or st["we"]) else 0.0)
    if "quiet" in codes:
        parts.append(0.0)
    p = 100.0 * sum(parts) / len(parts)
    if max_d > 0:
        p = max(p, 100.0 * age / max_d)
    return round(min(p, 99.0), 1)


def _shift_pct(sh: dict, floor: float) -> int:
    return int(round(100 * (sh["v"] - sh["e"]) / max(sh["e"], floor * sh["hours"])))


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
    def __init__(self, kb, live=None, every: float = 60.0, learning_days: float | None = None, learning_mode: str | None = None,
                 learning_min_days: float | None = None):
        self.kb, self.live, self.every = kb, live, every
        self._learning_days = learning_days                 # None: from settings (anomaly_learning_days); 0 switches learning off
        self._learning_mode = learning_mode                 # None: from settings (anomaly_learning_mode): dinamik | sabit
        self._min_days = learning_min_days                  # None: from settings (anomaly_learning_min_days)
        self._lcfg: dict | None = None
        self._learn_memo: dict = {}
        self._sig: dict = {}                                # (host, env) -> {"hour", "at", "hr", "rows": {signal: series}}
        self._ov: dict | None = None
        self._slo_cache: dict | None = None                 # closed rollup hours of every host, read once per hour
        self._season: dict = {}
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
        kb._exec("CREATE TABLE IF NOT EXISTS anomaly_learning (host TEXT NOT NULL, env TEXT NOT NULL, started TEXT, graduated_at TEXT DEFAULT '', PRIMARY KEY (host, env))")
        kb._exec("""CREATE TABLE IF NOT EXISTS anomaly_signal (host TEXT NOT NULL, env TEXT NOT NULL, sig TEXT NOT NULL, status TEXT DEFAULT 'learning',
            started TEXT, graduated_at TEXT DEFAULT '', relearned_at TEXT DEFAULT '', note TEXT DEFAULT '{}', state TEXT DEFAULT '{}', updated_at TEXT,
            PRIMARY KEY (host, env, sig))""")

    # ---------------------------------------------------------------- learning mode
    def _learn_cfg(self) -> dict:
        """mode, days (sabit: the fixed span; dinamik: the upper bound, at most 30) and min days; settings are re-read every 30 s."""
        c = self._lcfg
        if c is not None and c["at"] > time.time() - 30:
            return c
        cfg: dict = {}
        try:
            from . import settings
            cfg = settings.load()
            days = float(cfg.get("anomaly_learning_days", 7) or 0)
        except Exception:  # noqa: BLE001
            days = 7.0
        if self._learning_days is not None:
            days = float(self._learning_days)
        mode = str(self._learning_mode or cfg.get("anomaly_learning_mode") or "dinamik").strip().lower()
        mode = mode if mode in LEARN_MODES else "dinamik"
        try:
            mn = float(self._min_days if self._min_days is not None else (cfg.get("anomaly_learning_min_days", 1) or 0))
        except (TypeError, ValueError):
            mn = 1.0
        mx = min(days, LEARN_CAP_DAYS) if mode == "dinamik" else days
        self._lcfg = c = {"at": time.time(), "mode": mode, "max": mx, "min": max(0.0, min(mn, mx))}
        return c

    def reset_learning_cache(self) -> None:
        """Forget cached settings and learned states (after a settings change or an operator action)."""
        self._lcfg, self._ov, self._slo_cache = None, None, None
        self._learn_memo.clear()
        self._sig.clear()

    def learning_days(self) -> float:
        return self._learn_cfg()["max"]

    def learning_mode(self) -> str:
        return self._learn_cfg()["mode"]

    def _dynamic(self) -> bool:
        c = self._learn_cfg()
        return c["mode"] == "dinamik" and c["max"] > 0

    def learning(self, host: str, env: str, now: datetime | None = None, signal: str | None = None) -> dict:
        """{learning: bool, started, ends, pct, ...} for one host, or for one of its signals (rate, errors, cpu, ...).
        sabit: the host learns for `anomaly_learning_days` from its first minute in the rollups (signal is ignored).
        dinamik: per-signal maturity (see the module doc); the host is learning while any key signal is."""
        now = now or datetime.now(UTC)
        if not self._dynamic():
            return self._learning_fixed(host, env, now)
        m = self._refresh(host, env, now)
        if signal is not None and signal in m["rows"]:
            return {"host": host, "env": env, **self._summary(signal, m["rows"][signal], now)}
        return self._host_summary(host, env, m, now)

    def _host_row(self, host: str, env: str, now: datetime) -> dict:
        """The host's learning row (created on first sight; started = its first minute in the rollups, so hosts that already have
        history are not sent back to school after an upgrade)."""
        key = (host, env)
        memo = self._learn_memo.get(key)
        if memo is None or memo["at"] < time.time() - 300:
            rows = self.kb._exec("SELECT started, graduated_at FROM anomaly_learning WHERE host=? AND env=?", (host, env))
            if rows:
                started, grad = rows[0]["started"], rows[0]["graduated_at"] or ""
            else:
                first = self.kb._exec("SELECT MIN(minute) AS m FROM slo_minute WHERE host=? AND env=?", (host, env))[0]["m"] if self._has_rollups() else None
                started = (datetime.fromisoformat(first + ":00").replace(tzinfo=UTC) if first else now).isoformat(timespec="seconds")
                grad = ""
                self.kb._exec("INSERT INTO anomaly_learning (host, env, started, graduated_at) VALUES (?,?,?,'')" + self.kb.upsert_suffix("host, env", "started=anomaly_learning.started"), (host, env, started))
            memo = self._learn_memo[key] = {"started": started, "grad": grad, "at": time.time()}
        return memo

    def _learning_fixed(self, host: str, env: str, now: datetime) -> dict:
        memo = self._host_row(host, env, now)
        days = self.learning_days()
        started = datetime.fromisoformat(memo["started"])
        ends = started + timedelta(days=days)
        done = bool(memo["grad"]) or days <= 0 or now >= ends
        pct = 100.0 if done else round(100 * (now - started).total_seconds() / max(1.0, days * 86400), 1)
        return {"host": host, "env": env, "learning": not done, "started": memo["started"], "ends": ends.isoformat(timespec="seconds"), "pct": pct,
                "graduated_at": memo["grad"]}

    def _has_rollups(self) -> bool:
        if not hasattr(self, "_rollups_ok"):
            self._rollups_ok = "slo_minute" in self.kb.tables()
        return self._rollups_ok

    # ---- dynamic learning: one row per (host, env, signal) in anomaly_signal
    def _load_series(self, host: str, env: str) -> dict:
        out = {}
        for r in self.kb._exec("SELECT sig, status, started, graduated_at, relearned_at, note, state FROM anomaly_signal WHERE host=? AND env=?", (host, env)):
            try:
                st, note = {**blank_state(), **json.loads(r["state"] or "{}")}, json.loads(r["note"] or "{}")
            except ValueError:
                st, note = blank_state(), {}
            out[r["sig"]] = {"status": r["status"], "started": r["started"], "graduated_at": r["graduated_at"] or "", "relearned_at": r["relearned_at"] or "",
                             "note": note, "state": st}
        return out

    def _save(self, host: str, env: str, sig: str, row: dict) -> None:
        row.pop("dirty", None)
        self.kb._exec("INSERT INTO anomaly_signal (host, env, sig, status, started, graduated_at, relearned_at, note, state, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                      + self.kb.upsert_suffix("host, env, sig", "status=excluded.status, started=excluded.started, graduated_at=excluded.graduated_at, "
                                              "relearned_at=excluded.relearned_at, note=excluded.note, state=excluded.state, updated_at=excluded.updated_at"),
                      (host, env, sig, row["status"], row["started"], row["graduated_at"], row["relearned_at"], json.dumps(row["note"]),
                       json.dumps(row["state"], separators=(",", ":")), _now()))

    def _new_series(self, sig: str, hr: dict, now: datetime) -> dict:
        """A series seen for the first time. It is born learned when its host was already graduated (operator, or an earlier version's
        learning week) or has more history than the upper bound: existing hosts are never sent back to school by an upgrade."""
        c = self._learn_cfg()
        ts = now.isoformat(timespec="seconds")
        old = bool(hr["grad"]) or _age(hr["started"], now) >= c["max"]
        note = ({"code": "kept"} if hr["grad"] else {"code": "history", "days": round(_age(hr["started"], now), 1)}) if old else {}
        return {"status": "learned" if old else "learning", "started": hr["started"] if sig in SLO_SIGNALS else ts, "graduated_at": ts if old else "",
                "relearned_at": "", "note": note, "state": blank_state(), "dirty": True}

    def _refresh(self, host: str, env: str, now: datetime, force: bool = False) -> dict:
        """Read the closed hours since the last pass into the host's series and re-judge them (at most once per hour and host)."""
        key, hk = (host, env), _hk(now)
        m = self._sig.get(key)
        if m is not None and not force and m["hour"] == hk and m["at"] > time.time() - 300:
            return m
        hr = self._host_row(host, env, now)
        rows = self._load_series(host, env)
        end = _hour(now - timedelta(minutes=DIGEST_LAG_MIN))
        if self._has_rollups() and not all(s in rows for s in SLO_SIGNALS) and self._slo_rows(host, env, end - timedelta(hours=2), end):
            for s in SLO_SIGNALS:
                rows.setdefault(s, self._new_series(s, hr, now))
        self._digest_slo(host, env, rows, now)
        for sig, row in rows.items():
            self._evaluate(host, env, sig, row, now, rows)
        for sig, row in rows.items():
            if row.get("dirty"):
                self._save(host, env, sig, row)
        if len(self._sig) > 20000:
            self._sig.clear()
        m = self._sig[key] = {"hour": hk, "at": time.time(), "hr": hr, "rows": rows}
        return m

    @staticmethod
    def _start(row: dict, end: datetime) -> datetime:
        st = row["state"]
        if st["last"]:
            return _hdt(st["last"]) + timedelta(hours=1)
        lim = SEED_DAYS if row["status"] == "learned" else LEARN_CAP_DAYS + 1
        return max(_hour(datetime.fromisoformat(row["started"])), end - timedelta(days=lim))

    def _slo_query(self, start: datetime, end: datetime, host: str | None = None, env: str | None = None) -> dict:
        sql = ("SELECT host, env, substr(minute, 1, 13) AS h, SUM(total) AS t, SUM(errors) AS e, MIN(minute) AS f FROM slo_minute WHERE minute >= ? AND minute < ?"
               + (" AND host=? AND env=?" if host is not None else "") + " GROUP BY host, env, substr(minute, 1, 13)")
        args = (start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")) + ((host, env) if host is not None else ())
        out: dict = {}
        for r in self.kb._exec(sql, args):
            out.setdefault((r["host"], r["env"]), {})[r["h"]] = r
        return out

    def _slo_rows(self, host: str, env: str, t: datetime, end: datetime) -> dict:
        """One host's closed hours [t, end) from a batch read once per hour for every host (the rollups have no host index: one
        range scan per hour instead of one per host). A host that needs older hours widens the batch (at least to a week)."""
        c = self._slo_cache
        if c is None or c["end"] != end or t < c["start"]:
            start = t if t >= end - timedelta(hours=2) else min(t, end - timedelta(days=SEED_DAYS))      # a backfill reads a week at once
            start = min(start, end - timedelta(hours=2), c["start"] if c is not None and c["end"] == end else start)
            self._slo_cache = c = {"end": end, "start": start, "by": self._slo_query(start, end)}
        return {h: r for h, r in c["by"].get((host, env), {}).items() if h >= _hk(t)}

    def _digest_slo(self, host: str, env: str, rows: dict, now: datetime) -> None:
        """rate / errors: per-minute means of each closed hour from slo_minute. Quiet minutes are real zeros, and so is a quiet hour
        up to 24 h after the host last logged (later it is 'not observed': agent gone, host off)."""
        sers = [(s, rows[s]) for s in SLO_SIGNALS if s in rows]
        if not sers or not self._has_rollups():
            return
        end = _hour(now - timedelta(minutes=DIGEST_LAG_MIN))
        starts = {s: self._start(r, end) for s, r in sers}
        t = min(starts.values())
        if t >= end:
            return
        by = self._slo_rows(host, env, t, end)
        seen = max((r["state"].get("seen") or "" for _, r in sers), default="")
        while t < end:
            hk = _hk(t)
            r = by.get(hk)
            vals = None
            if r is not None:
                mins = 60 if seen else max(1, 60 - int(str(r["f"])[14:16] or 0))      # the host's first hour counts from its first minute
                vals = {"rate": float(r["t"] or 0) / mins, "errors": float(r["e"] or 0) / mins}
                seen = hk
            elif seen and t - _hdt(seen) <= timedelta(hours=24):
                vals = {"rate": 0.0, "errors": 0.0}
            for s, row in sers:
                if t < starts[s]:
                    continue
                if vals is not None:
                    fold(row["state"], hk, vals[s], row["status"] == "learned", _floor(s))
                else:
                    row["state"]["last"] = hk
                row["dirty"] = True
            t += timedelta(hours=1)
        for _, row in sers:
            row["state"]["seen"] = seen

    def observe_metrics(self, per_minute: list[dict], now: datetime | None = None) -> None:
        """Metric series (cpu, memory, disk, gpu, ...): each closed hour of the last two with at least METRIC_MIN_SAMPLES minutes
        of samples is one observation (its mean). Called by the scan with the live per-minute metric means."""
        if not self._dynamic() or not per_minute:
            return
        now = now or datetime.now(UTC)
        end = _hour(now)
        low = end - timedelta(hours=2)
        groups: dict[tuple, dict] = {}
        for r in per_minute:
            m = r["minute"]
            if not isinstance(m, datetime):
                m = datetime.fromisoformat(str(m))
            if m.tzinfo is None:
                m = m.replace(tzinfo=UTC)
            groups.setdefault((r["host"], r.get("env") or "unknown", r["metric"]), {}).setdefault(_hk(m), []).append(float(r["value"]))
        for (host, env, metric), byh in groups.items():
            if not host or host == "-":
                continue
            mm = self._refresh(host, env, now)
            row = mm["rows"].get(metric)
            if row is None:
                row = mm["rows"][metric] = self._new_series(metric, mm["hr"], now)
            st = row["state"]
            for hk in sorted(byh):
                t = _hdt(hk)
                if t >= end or t < low or (st["last"] and hk <= st["last"]):
                    continue
                if len(byh[hk]) >= METRIC_MIN_SAMPLES:
                    fold(st, hk, sum(byh[hk]) / len(byh[hk]), row["status"] == "learned", METRIC_FLOOR)
                else:
                    st["last"] = hk
                row["dirty"] = True
            self._evaluate(host, env, metric, row, now, mm["rows"])
            if row.get("dirty"):
                self._save(host, env, metric, row)
                self._ov = None

    def _evaluate(self, host: str, env: str, sig: str, row: dict, now: datetime, rows: dict) -> None:
        """Learning -> learned when nothing is missing (or at the upper bound); learned -> learning when a 24 h level shift is not an incident."""
        c = self._learn_cfg()
        st, floor, ts = row["state"], _floor(sig), now.isoformat(timespec="seconds")
        if row["status"] == "learned":
            sh = st.get("shift")
            if not sh or sh.get("incident") or not st["last"]:
                return
            span = (_hdt(st["last"]) - _hdt(sh["since"])).total_seconds() / 3600 + 1
            if span < SHIFT_HOURS or sh["hours"] < 0.75 * span:
                return
            why = self._incident(host, env, sig, sh, rows)
            if why:                                   # a shift that comes with errors / alerts is an incident: it stays an anomaly
                sh["incident"] = why
                row["dirty"] = True
                return
            pct, since = _shift_pct(sh, floor), _hdt(sh["since"]).isoformat(timespec="seconds")
            new = blank_state()
            for hk, v in st["recent"]:                # the new level since the shift is the first thing it learns
                if hk >= sh["since"]:
                    fold(new, hk, v, False, floor)
            new["last"], new["seen"] = st["last"], st.get("seen", "")
            new["relearn"] = {"since": since, "pct": pct, "n": int((st.get("relearn") or {}).get("n", 0)) + 1}
            row.update(status="learning", started=since, graduated_at="", relearned_at=ts, note={"code": "shift", "pct": pct, "since": since},
                       state=new, dirty=True)
            return
        age = _age(row["started"], now)
        reasons = _reasons(st, sig, age, c["min"])
        if not reasons:
            row.update(status="learned", graduated_at=ts, dirty=True,
                       note={"code": "learned", "cov": sum(1 for x in st["hours"] if x), "delta": round((st["delta"] or 0) * 100, 1)})
        elif age >= c["max"]:
            row.update(status="learned", graduated_at=ts, dirty=True, note={"code": "cap", "days": c["max"], "open": [r["code"] for r in reasons]})

    def _incident(self, host: str, env: str, sig: str, sh: dict, rows: dict) -> str:
        """Why a level shift is not a new normal: more errors, (near) silence, an errors shift on the host, or alerts since it began."""
        if sig == "errors" and sh["dir"] > 0:
            return "errors"
        if sh["dir"] < 0 and sh["v"] <= 0.1 * sh["e"]:
            return "silence"
        es = rows.get("errors")
        if sig != "errors" and es and ((es["state"].get("shift") or {}).get("dir") or 0) > 0:
            return "errors"
        since = _hdt(sh["since"]).isoformat(timespec="seconds")
        for r in self.kb._exec("SELECT kind, key FROM anomalies WHERE host=? AND env=? AND last_seen >= ? AND status IN ('open', 'ack', 'resolved')", (host, env, since)):
            if r["kind"] in ("errors", "silence") or str(r["key"]).startswith("threshold:"):
                return "alert"
        return ""

    def _summary(self, sig: str, row: dict, now: datetime) -> dict:
        c = self._learn_cfg()
        st = row["state"]
        learning = row["status"] != "learned"
        age = _age(row["started"], now)
        reasons = _reasons(st, sig, age, c["min"]) if learning else []
        out = {"signal": sig, "key": sig in KEY_SIGNALS, "learning": learning, "status": row["status"], "started": row["started"],
               "ends": (datetime.fromisoformat(row["started"]) + timedelta(days=c["max"])).isoformat(timespec="seconds"),
               "graduated_at": row["graduated_at"], "pct": _progress(st, age, c["min"], c["max"], reasons) if learning else 100.0,
               "reasons": reasons, "note": row["note"], "coverage": sum(1 for x in st["hours"] if x),
               "delta": None if st["delta"] is None else round(st["delta"] * 100, 1), "weekly": _weekly(st, _floor(sig)),
               "days": {"wd": st["wd"], "we": st["we"]}, "relearned_at": row.get("relearned_at", "")}
        sh = st.get("shift")
        if sh and sh["hours"] >= 3:
            out["shift"] = {"dir": sh["dir"], "since": _hdt(sh["since"]).isoformat(timespec="seconds"), "hours": sh["hours"],
                            "pct": _shift_pct(sh, _floor(sig)), "incident": sh.get("incident", "")}
        return out

    def _host_summary(self, host: str, env: str, m: dict, now: datetime) -> dict:
        c, hr = self._learn_cfg(), m["hr"]
        rank = {s: i for i, s in enumerate(_SIG_ORDER)}
        sigs = [self._summary(s, m["rows"][s], now) for s in sorted(m["rows"], key=lambda s: (rank.get(s, 99), s))]
        keys = [s for s in sigs if s["key"]] or sigs
        ends = (datetime.fromisoformat(hr["started"]) + timedelta(days=c["max"])).isoformat(timespec="seconds")
        if keys:
            lr = [s for s in keys if s["learning"]]
            learning, pct = bool(lr), (min(s["pct"] for s in lr) if lr else 100.0)
            ends = max(s["ends"] for s in lr) if lr else ends
        else:                                          # nothing observed yet: the upper bound (or an operator) decides
            done = bool(hr["grad"]) or _age(hr["started"], now) >= c["max"]
            learning, pct = not done, (100.0 if done else round(min(99.0, 100 * _age(hr["started"], now) / max(c["max"], 1e-9)), 1))
        return {"host": host, "env": env, "mode": "dinamik", "learning": learning, "started": hr["started"], "ends": ends, "pct": pct,
                "graduated_at": hr["grad"], "signals": sigs}

    def host_learning(self, host: str, env: str = "", now: datetime | None = None) -> dict:
        """One host's learning for the server detail (never creates a row for a host the tracker has not seen)."""
        rows = self.kb._exec("SELECT env FROM anomaly_learning WHERE host=?" + (" AND env=?" if env else "") + " ORDER BY started DESC LIMIT 1",
                             (host, env) if env else (host,))
        if not rows:
            return {"host": host, "env": env, "known": False, "mode": self.learning_mode()}
        return {**self.learning(host, rows[0]["env"], now), "known": True, "mode": self.learning_mode()}

    def graduate(self, host: str, env: str, signal: str | None = None) -> dict:
        """End a host's learning now (operator decision); with `signal`, only that series."""
        now = datetime.now(UTC)
        ts = now.isoformat(timespec="seconds")
        self.learning(host, env)
        if signal:
            m = self._refresh(host, env, now, force=True)
            row = m["rows"].get(signal)
            if row is None:
                raise KeyError(signal)
            if row["status"] != "learned":
                row.update(status="learned", graduated_at=ts, note={"code": "operator"})
                self._save(host, env, signal, row)
        else:
            self.kb._exec("UPDATE anomaly_learning SET graduated_at=? WHERE host=? AND env=?", (_now(), host, env))
            self.kb._exec("UPDATE anomaly_signal SET status='learned', graduated_at=?, note=? WHERE host=? AND env=? AND status <> 'learned'",
                          (ts, json.dumps({"code": "operator"}), host, env))
        self._learn_memo.pop((host, env), None)
        self._sig.pop((host, env), None)
        self._ov = None
        return self.learning(host, env)

    def relearn(self, host: str, env: str, signal: str, now: datetime | None = None) -> dict:
        """Send one signal of a host back to learning from now (operator: 'its normal changed')."""
        now = now or datetime.now(UTC)
        ts = now.isoformat(timespec="seconds")
        m = self._refresh(host, env, now, force=True)
        row = m["rows"].get(signal)
        if row is None:
            raise KeyError(signal)
        old = row["state"]
        st = blank_state()
        st["last"], st["seen"] = old["last"], old.get("seen", "")
        st["relearn"] = {"since": ts, "pct": None, "n": int((old.get("relearn") or {}).get("n", 0)) + 1}
        row.update(status="learning", started=ts, graduated_at="", relearned_at=ts, note={"code": "manual"}, state=st)
        self._save(host, env, signal, row)
        self._sig.pop((host, env), None)
        self._ov = None
        return self.learning(host, env, now)

    def restart_learning(self, host: str | None = None, env: str | None = None) -> int:
        """Send one host (or every host) back to learning from now, e.g. after a big release changed what normal is."""
        now = _now()
        if host:
            self.kb._exec("UPDATE anomaly_learning SET started=?, graduated_at='' WHERE host=? AND env=?", (now, host, env or ""))
            self.kb._exec("DELETE FROM anomaly_signal WHERE host=? AND env=?", (host, env or ""))
        else:
            self.kb._exec("UPDATE anomaly_learning SET started=?, graduated_at=''", (now,))
            self.kb._exec("DELETE FROM anomaly_signal")
        self._learn_memo.clear()
        self._sig.clear()
        self._ov = None
        return int(self.kb._exec("SELECT COUNT(*) AS n FROM anomaly_learning WHERE started=?", (now,))[0]["n"])

    def learning_overview(self, now: datetime | None = None) -> dict:
        c = self._learn_cfg()
        days = c["max"]
        since = ((now or datetime.now(UTC)) - timedelta(days=max(days, 1))).isoformat(timespec="seconds")
        shadow = int(self.kb._exec("SELECT COUNT(*) AS n FROM anomalies WHERE status='learning' AND last_seen >= ?", (since,))[0]["n"])
        if not self._dynamic():
            hosts = [self.learning(r["host"], r["env"]) for r in self.kb._exec("SELECT host, env FROM anomaly_learning ORDER BY started DESC LIMIT 2000")]
            return {"mode": c["mode"], "days": days, "learning": [h for h in hosts if h["learning"]], "learned": sum(1 for h in hosts if not h["learning"]), "shadow": shadow}
        ov = self._ov
        if now is not None:
            ov = {"val": self._overview_dynamic(now)}
        elif ov is None or ov["at"] < time.time() - 20:
            ov = self._ov = {"at": time.time(), "val": self._overview_dynamic(datetime.now(UTC))}
        return {**ov["val"], "mode": "dinamik", "days": days, "min_days": c["min"], "shadow": shadow}

    def _overview_dynamic(self, now: datetime) -> dict:
        """Every host from the stored series in two queries (no history is read here; the scan does that)."""
        c = self._learn_cfg()
        per: dict[tuple, dict] = {}
        for r in self.kb._exec("SELECT host, env, sig, status FROM anomaly_signal"):
            per.setdefault((r["host"], r["env"]), {})[r["sig"]] = r["status"]
        hosts, learned = [], 0
        for r in self.kb._exec("SELECT host, env, started, graduated_at FROM anomaly_learning ORDER BY started DESC LIMIT 2000"):
            key = (r["host"], r["env"])
            st = per.get(key, {})
            keys = [s for s in st if s in KEY_SIGNALS] or list(st)
            if keys:
                busy = any(st[s] != "learned" for s in keys)
            else:
                busy = not (r["graduated_at"] or _age(r["started"], now) >= c["max"])
            if not busy:
                learned += 1
                continue
            m = self._sig.get(key) or {"hr": {"started": r["started"], "grad": r["graduated_at"] or ""}, "rows": self._load_series(*key)}
            hosts.append(self._host_summary(r["host"], r["env"], m, now))
        week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        events = []
        for r in self.kb._exec("SELECT host, env, sig, status, relearned_at, note, state FROM anomaly_signal WHERE relearned_at >= ? ORDER BY relearned_at DESC LIMIT 20", (week,)):
            try:
                rl = json.loads(r["state"] or "{}").get("relearn") or {}
            except ValueError:
                rl = {}
            events.append({"host": r["host"], "env": r["env"], "signal": r["sig"], "at": r["relearned_at"], "since": rl.get("since", r["relearned_at"]),
                           "pct": rl.get("pct"), "manual": rl.get("pct") is None, "learning": r["status"] != "learned"})
        return {"learning": hosts, "learned": learned, "events": events}

    def _seasonal(self, host: str, env: str, now: datetime) -> dict | None:
        """Per-minute totals and errors of this host at the same weekday and hour (± 1 h) over the last 4 weeks, cached per hour."""
        key = (host, env, now.strftime("%Y-%m-%dT%H"))
        if key in self._season:
            return self._season[key]
        if len(self._season) > 5000:
            self._season.clear()
        start = (now - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M")
        cut = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        rows = self.kb._exec("SELECT minute, SUM(total) AS t, SUM(errors) AS e FROM slo_minute WHERE host=? AND env=? AND minute >= ? AND minute < ? GROUP BY minute",
                             (host, env, start, cut))
        tot, err = [], []
        for r in rows:
            m = datetime.fromisoformat(r["minute"] + ":00")
            if m.weekday() == now.weekday() and abs(m.hour - now.hour) <= 1:
                tot.append(float(r["t"])); err.append(float(r["e"]))
        out = {"rate": tot, "errors": err} if len(tot) >= 60 else None
        self._season[key] = out
        return out

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
        per_host: dict[tuple[str, str], list[int]] = {}
        services: dict[tuple[str, str], dict] = {}
        ru = getattr(self.live, "rollup", None)
        live_now = abs((datetime.now(UTC) - now).total_seconds()) < 90
        obs = [] if (ru is not None and live_now and ru.added) else self._window(start)
        if not obs and ru is not None and live_now and ru.added:      # counted at ingest: no pass over 100k+ events
            per_host, services = ru.host_counts(WINDOW_MIN)
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
                learn = self.learning(host, env, now, kind)["learning"]               # dinamik: this signal's maturity; sabit: the host's
                season = None if learn else self._seasonal(host, env, now)
                med, mad = robust(series)
                if season:                                             # learned: the same hour of the same weekday is normal too
                    smed, smad = robust(season[kind])
                    med, mad = max(med, smed), max(mad, smad)
                med5, mad5 = med * WINDOW_MIN, mad * (WINDOW_MIN ** 0.5)
                z = zscore(value, med5, mad5)
                if value >= MIN_EVENTS[kind] and z >= Z_LIMIT and value >= 2 * med5:
                    top = sorted(services.get((host, env), {}).items(), key=lambda kv: -kv[1])[:3]
                    detail = (f"{value:.0f} {'ERROR+' if kind == 'errors' else 'events'} in {WINDOW_MIN} min; normal for this host is {med5:.0f} ± {mad5:.0f}"
                              + (f"; top services: " + ", ".join(f"{s} ({n})" for s, n in top) if top else ""))
                    out.append(self._upsert(kind, f"{kind}:{env}:{host}", env=env, host=host, service=top[0][0] if top else "", learning=learn,
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
            out.append(self._upsert("silence", f"silence:{env}:{host}", env=env, host=host, learning=self.learning(host, env, now, "rate")["learning"], title=f"{host}: silent for {quiet:.0f} min (normally {per_min:.1f} events / min)",
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
                                        learning=self.learning(c["host"], c["env"], now)["learning"] if c["host"] else False,
                                        title=f"New {c['sev']} pattern × {c['n']}: {tpl[:90]}",
                                        detail=f"First seen {ts.replace('T', ' ')} on {c['host'] or '-'} / {c['service'] or '-'}; never seen in the last days",
                                        observed=c["n"], baseline=0.0, spread=0.0, score=min(c["n"] / MIN_EVENTS["pattern"] * 2, 10.0), series=[]))
        return out

    def _scan_metrics(self, now: datetime) -> list[dict]:
        ms = self.live.metric_stats(180) if hasattr(self.live, "metric_stats") else {}
        try:
            self.observe_metrics(ms.get("per_minute", []), now)           # dinamik learning: the metric series learn from the same samples
        except Exception:  # noqa: BLE001 - learning never stops detection
            pass
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
                out.append(self._upsert("metric", f"metric:{env}:{host}:{metric}", env=env, host=host, metric=metric, learning=self.learning(host, env, now, metric)["learning"],
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
        limit_fn = getattr(self, "limit_fn", None)                 # per-server policies (thresholds.py) when the app wired them
        for (host, metric), (_m, value, env) in latest.items():
            limit = limit_fn(host, metric, env) if limit_fn is not None else thr.get(metric)
            if limit is None or value < limit:
                continue
            out.append(self._upsert("metric", f"threshold:{env}:{host}:{metric}", env=env, host=host, metric=metric,
                                    title=f"{host}: {metric} {value:.0f}% ≥ {limit:.0f}%",
                                    detail=f"{metric} is at {value:.0f}% on {host} ({env}); the alert threshold is {limit:.0f}% (System › Settings)",
                                    observed=value, baseline=float(limit), spread=0.0, score=round((value - limit) / max(1.0, 100 - limit) * 5 + 3, 2), series=[value]))
        return out

    # ---------------------------------------------------------------- store
    def _upsert(self, kind: str, key: str, *, env: str, host: str, title: str, detail: str, observed: float, baseline: float, spread: float,
                score: float, series: list, service: str = "", metric: str = "", learning: bool = False) -> dict:
        now = _now()
        live_states = "('learning')" if learning else "('open', 'ack')"
        rows = self.kb._exec(f"SELECT id, hits FROM anomalies WHERE key=? AND status IN {live_states} ORDER BY id DESC LIMIT 1", (key,))
        ser = json.dumps([round(float(v), 2) for v in series][-61:])
        if rows:
            self.kb._exec("UPDATE anomalies SET title=?, detail=?, observed=?, baseline=?, spread=?, score=?, hits=hits+1, last_seen=?, cleared_at='', series=?, updated_at=? WHERE id=?",
                          (title, detail, float(observed), float(baseline), float(spread), round(float(score), 2), now, ser, now, rows[0]["id"]))
            return self.get(rows[0]["id"])
        steps = json.dumps([{"id": s, "done": False, "ts": ""} for s in STEPS[kind]])
        aid = self.kb._insert("INSERT INTO anomalies (kind, key, env, host, service, metric, title, detail, observed, baseline, spread, score, hits, first_seen, last_seen, status, steps, series, updated_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
                              (kind, key, env, host, service, metric, title, detail, float(observed), float(baseline), float(spread), round(float(score), 2), now, now,
                               "learning" if learning else "open", steps, ser, now))
        row = self.get(aid)
        if not learning and self.on_open is not None:            # the company memory learns from every anomaly that really opened
            try:
                self.on_open(row)
            except Exception:  # noqa: BLE001
                pass
        return row

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
        try:
            out["learning"] = self.learning_overview()
        except Exception:  # noqa: BLE001
            out["learning"] = {"mode": self.learning_mode(), "days": self.learning_days(), "learning": [], "learned": 0, "shadow": 0, "events": []}
        return out
