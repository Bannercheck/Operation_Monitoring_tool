"""Per-server threshold policies and their alerts.

A policy says: for these servers (one host, a name pattern, a business application, a pool, an environment, or everyone), this
metric has a warning and a critical level, it must stay beyond the level for N minutes before anyone is told, and the alert goes to
these people / groups over these channels (e-mail, SMS, webhook: Teams, Slack or any JSON endpoint). When several policies match a
server, the most specific one wins for that metric: host > host pattern > application > pool > environment > all (a team's own
policy first, see _rank); the global System › Settings thresholds (thr_cpu …) are the fallback for CPU / memory / disk / GPU.

Application scope (watchover.applications): the policy covers the application's member hosts (explicit, inventory application value,
pool). It sits below host and host pattern (a named server is more specific than the group it serves) and above pool and
environment (an application is the business unit, a pool only a technical one). A host that serves several applications gets the
strictest of their application policies (the lowest level wins a tie), so a shared server meets every application's demand.
Alerts name the applications of the server.

Metrics, all "higher is worse":
  cpu, memory, disk, gpu   latest agent reading, %          error_rate   ERROR+ share of the server's lines, last 5 min, %
  errors_per_min           ERROR+ lines per minute, 5 min    p95_ms       95th percentile of "<n> ms" durations, last 5 min
  silent_min               minutes since the server's last log line (a server that stopped logging)

Every minute the evaluator computes each server's values, moves a small state machine per (server, metric): ok -> pending (beyond a
level, timer running) -> warn / crit (sustained: notify once, re-notify only after the cooldown or on escalation) -> ok again when
the value is back under the warning level less 5 % (hysteresis), with a "resolved" notice if an alert went out. Breaches also open a
'metric' anomaly so the operations screen and the bell see them.
"""
from __future__ import annotations

import fnmatch
import json
import threading
import time
from datetime import datetime, timezone

UTC = timezone.utc
METRICS = {
    "cpu": {"unit": "%", "label": "CPU"}, "memory": {"unit": "%", "label": "Bellek / Memory"}, "disk": {"unit": "%", "label": "Disk"},
    "gpu": {"unit": "%", "label": "GPU"}, "error_rate": {"unit": "%", "label": "Hata oranı / Error rate"},
    "errors_per_min": {"unit": "/min", "label": "Hata/dk / Errors/min"}, "p95_ms": {"unit": "ms", "label": "p95 gecikme / latency"},
    "silent_min": {"unit": "min", "label": "Sessizlik / Silence"},
}
SCOPES = ("host", "glob", "application", "pool", "env", "all")
SPECIFICITY = {"host": 6, "glob": 5, "application": 4, "pool": 3, "env": 2, "all": 1, "global": 0}
CHANNELS = ("email", "sms", "webhook")
WINDOW_MIN = 5
HYSTERESIS = 0.95


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Thresholds:
    def __init__(self, kb, live, notifier=None, anomalies=None, globals_fn=None):
        self.kb, self.live, self.notifier, self.anomalies = kb, live, notifier, anomalies
        self.globals_fn = globals_fn                   # -> {"cpu": 85, ...} from System › Settings
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.runs, self.last_run, self.last_ms = 0, "", 0.0
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS threshold_policies (id {pk}, name TEXT NOT NULL, scope TEXT NOT NULL, scope_value TEXT DEFAULT '',
            metric TEXT NOT NULL, warn REAL, crit REAL, for_min INTEGER DEFAULT 3, channels TEXT DEFAULT 'email', targets TEXT DEFAULT '',
            cooldown_min INTEGER DEFAULT 30, notify_resolved INTEGER DEFAULT 1, enabled INTEGER DEFAULT 1, note TEXT DEFAULT '', updated_at TEXT)""")
        kb._exec("""CREATE TABLE IF NOT EXISTS threshold_state (host TEXT NOT NULL, metric TEXT NOT NULL, level TEXT NOT NULL, since TEXT, value REAL,
            limit_value REAL, policy_id INTEGER, notified_level TEXT DEFAULT '', notified_at TEXT DEFAULT '', updated_at TEXT, PRIMARY KEY (host, metric))""")
        from .demo import ensure_column
        ensure_column(kb, "threshold_state")                 # 1 = the state of a host only simulator input wrote (demo.py purges these)
        try:
            if "team_id" not in kb.columns("threshold_policies"):
                kb._exec("ALTER TABLE threshold_policies ADD COLUMN team_id INTEGER")   # a team's policy (NULL: installation-wide)
        except Exception:  # noqa: BLE001
            pass
        self._cache: tuple[float, list[dict]] = (0.0, [])
        self.team_hosts_fn = None                      # team id -> its hosts (set by access.wire): a team's policy covers only them
        self.apps_of_fn = None                         # host -> names of its applications (set by Services; watchover.applications)

    # ---------------------------------------------------------------- policies
    def list(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM threshold_policies ORDER BY metric, scope, scope_value, id")]

    def get(self, pid: int) -> dict:
        r = self.kb._exec("SELECT * FROM threshold_policies WHERE id=?", (pid,))
        if not r:
            raise KeyError(f"policy {pid}")
        return dict(r[0])

    def _check(self, p: dict) -> dict:
        if p.get("metric") not in METRICS:
            raise ValueError(f"metric must be one of {', '.join(METRICS)}")
        if p.get("scope") not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if p["scope"] != "all" and not str(p.get("scope_value") or "").strip():
            raise ValueError("scope_value is required for this scope")
        warn, crit = p.get("warn"), p.get("crit")
        if warn is None and crit is None:
            raise ValueError("set a warning level, a critical level or both")
        for v in (warn, crit):
            if v is not None and (float(v) < 0 or (METRICS[p["metric"]]["unit"] == "%" and float(v) > 100)):
                raise ValueError("levels must be 0-100 for percentages and not negative")
        if warn is not None and crit is not None and float(crit) < float(warn):
            raise ValueError("the critical level must not be below the warning level")
        chans = [c.strip() for c in str(p.get("channels") or "").split(",") if c.strip()]
        if any(c not in CHANNELS for c in chans):
            raise ValueError(f"channels: {', '.join(CHANNELS)}")
        return {"name": str(p.get("name") or "").strip() or f"{p['metric']} · {p['scope']} {p.get('scope_value') or ''}".strip(), "scope": p["scope"],
                "scope_value": "" if p["scope"] == "all" else str(p["scope_value"]).strip(), "metric": p["metric"],
                "warn": None if warn is None else float(warn), "crit": None if crit is None else float(crit), "for_min": max(0, min(int(p.get("for_min") or 0), 1440)),
                "channels": ",".join(chans), "targets": str(p.get("targets") or "").strip(), "cooldown_min": max(1, int(p.get("cooldown_min") or 30)),
                "notify_resolved": int(bool(p.get("notify_resolved", True))), "enabled": int(bool(p.get("enabled", True))), "note": str(p.get("note") or "")[:300]}

    def save(self, p: dict, pid: int | None = None) -> dict:
        """Insert / update. `team_id` in `p` (None: installation-wide) is stored with the row in the same statement, so a team's
        policy never covers other hosts, not even for one evaluation."""
        row = self._check(p)
        if "team_id" in p:
            row["team_id"] = None if p.get("team_id") in (None, "") else int(p["team_id"])
        cols = list(row)
        if pid is None:
            pid = self.kb._insert(f"INSERT INTO threshold_policies ({', '.join(cols)}, updated_at) VALUES ({', '.join('?' * len(cols))}, ?)", (*row.values(), _now()))
        else:
            self.get(pid)
            self.kb._exec(f"UPDATE threshold_policies SET {', '.join(f'{c}=?' for c in cols)}, updated_at=? WHERE id=?", (*row.values(), _now(), pid))
        self._cache = (0.0, [])
        return self.get(pid)

    def delete(self, pid: int) -> None:
        self.kb._exec("DELETE FROM threshold_policies WHERE id=?", (pid,))
        self._cache = (0.0, [])

    def _enabled(self) -> list[dict]:
        t, rows = self._cache
        if time.time() - t > 15:
            rows = [r for r in self.list() if r["enabled"]]
            self._cache = (time.time(), rows)
        return rows

    # ---------------------------------------------------------------- resolution
    def _pool(self, host: str) -> str:
        try:
            return self.live.rollup.pool_of(host)
        except Exception:  # noqa: BLE001
            return ""

    def _team_hosts(self, tid) -> set:
        if self.team_hosts_fn is None:
            return set()
        try:
            return self.team_hosts_fn(tid)
        except Exception:  # noqa: BLE001
            return set()

    def matches(self, p: dict, host: str, env: str, pool: str | None = None) -> bool:
        if p.get("team_id") is not None and host not in self._team_hosts(p["team_id"]):
            return False                                # a team's policy: only the team's hosts ("all" = all of them)
        v = (p.get("scope_value") or "").strip().lower()
        h = (host or "").lower()
        if p["scope"] == "all":
            return True
        if p["scope"] == "host":
            return h == v
        if p["scope"] == "glob":
            return fnmatch.fnmatchcase(h, v)
        if p["scope"] == "pool":
            return (pool if pool is not None else self._pool(host)).lower() == v
        if p["scope"] == "env":
            return (env or "unknown").lower() == v
        if p["scope"] == "application":
            return v in {a.lower() for a in self.apps_of(host)}
        return False

    def apps_of(self, host: str) -> list:
        if self.apps_of_fn is None or not host:
            return []
        try:
            return list(self.apps_of_fn(host) or [])
        except Exception:  # noqa: BLE001
            return []

    def effective(self, host: str, env: str = "", pool: str | None = None) -> dict[str, dict]:
        """metric -> the winning policy for this server (or the global fallback), with where it came from."""
        pool = pool if pool is not None else self._pool(host)
        out: dict[str, dict] = {}
        for p in self._enabled():
            if not self.matches(p, host, env, pool):
                continue
            cur = out.get(p["metric"])
            if cur is None or self._rank(p) > self._rank(cur):  # the owning team's policy beats installation defaults, then specificity, then newest
                out[p["metric"]] = p
        g = self.globals_fn() if self.globals_fn else {}
        for m in ("cpu", "memory", "disk", "gpu"):
            if m not in out and g.get(m) is not None:
                out[m] = {"id": None, "name": "System › Settings", "scope": "global", "scope_value": "", "metric": m, "warn": None, "crit": float(g[m]),
                          "for_min": 0, "channels": "", "targets": "", "cooldown_min": 30, "notify_resolved": 0}
        return out

    @staticmethod
    def _rank(p: dict) -> tuple:
        """Higher wins: the owning team's policy, then specificity, then (application scope only: a host in several applications)
        the stricter level, then the newest."""
        strict = 0.0
        if p["scope"] == "application":
            lv = p.get("warn") if p.get("warn") is not None else p.get("crit")
            strict = -float(lv) if lv is not None else 0.0
        return (1 if p.get("team_id") is not None else 0, SPECIFICITY[p["scope"]], strict, p.get("id") or 0)

    def limit_for(self, host: str, metric: str, env: str = "") -> float | None:
        """The level a reading is compared with for the operations screen's breach list: the warning level, else the critical one."""
        p = self.effective(host, env).get(metric)
        if not p:
            return None
        return p["warn"] if p.get("warn") is not None else p.get("crit")

    # ---------------------------------------------------------------- values
    def values(self) -> dict[str, dict]:
        """host -> {"env", "pool", metric: value} for every server seen in the last hour."""
        out: dict[str, dict] = {}
        try:
            ms = self.live.metric_stats(WINDOW_MIN)            # the newest reading per (metric, host), not a minute's average
            envs = {h: (x.get("env") or "unknown") for h, x in (ms.get("per_host") or {}).items()}
            for (m, h), v in (ms.get("latest") or {}).items():
                d = out.setdefault(h, {"env": envs.get(h, "unknown")})
                d[m] = round(float(v), 2)
        except Exception:  # noqa: BLE001
            pass
        r = self.live.rollup
        try:
            for row in r.host_table(WINDOW_MIN):
                d = out.setdefault(row["host"], {})
                d.setdefault("env", row["env"])
                d["pool"] = row["pool"]
                if row["events"]:
                    d["error_rate"] = round(100.0 * row["errors"] / row["events"], 3)
                d["errors_per_min"] = round(row["errors"] / WINDOW_MIN, 2)
                if row.get("last_seen_s") is not None:
                    d["silent_min"] = round(row["last_seen_s"] / 60, 1)
            from .rollup import BOUNDS, _q
            now_m = int(time.time() // 60)
            hist: dict[str, list[int]] = {}
            with r.lock:
                for m in range(now_m - WINDOW_MIN + 1, now_m + 1):
                    for (hn, _s), hh in r.lat.get(m, {}).items():
                        acc = hist.setdefault(hn, [0] * (len(BOUNDS) + 1))
                        for i, c in enumerate(hh):
                            acc[i] += c
            for hn, hh in hist.items():
                if sum(hh) >= 5:
                    out.setdefault(hn, {})["p95_ms"] = _q(hh, 0.95)
        except Exception:  # noqa: BLE001
            pass
        for h, d in out.items():
            d.setdefault("env", "unknown")
            d.setdefault("pool", self._pool(h))
        out.pop("-", None)
        return out

    # ---------------------------------------------------------------- evaluation
    @staticmethod
    def _level(p: dict, v: float) -> str:
        if p.get("crit") is not None and v >= p["crit"]:
            return "crit"
        if p.get("warn") is not None and v >= p["warn"]:
            return "warn"
        return "ok"

    def evaluate(self, now: float | None = None) -> list[dict]:
        """One pass: returns the notices sent (or attempted) in this pass."""
        t0 = time.perf_counter()
        now = now or time.time()
        now_iso = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")
        sent: list[dict] = []
        vals = self.values()
        state = {(s["host"], s["metric"]): dict(s) for s in self.kb._exec("SELECT * FROM threshold_state")}
        seen = set()
        with self.lock:
            for host, d in vals.items():
                eff = self.effective(host, d.get("env", ""), d.get("pool"))
                for metric, p in eff.items():
                    v = d.get(metric)
                    if v is None:
                        continue
                    seen.add((host, metric))
                    lvl = self._level(p, float(v))
                    st = state.get((host, metric))
                    low = p["warn"] if p.get("warn") is not None else p["crit"]
                    if lvl == "ok" and st and st["level"] != "ok" and float(v) >= low * HYSTERESIS:
                        lvl = st["level"] if st["level"] in ("warn", "crit") else "ok"          # inside the hysteresis band: hold
                    sent.extend(self._step(host, metric, d.get("env", ""), p, float(v), lvl, st, now, now_iso))
            for (host, metric), st in state.items():           # a metric that disappeared (agent stopped sending it): close quietly
                if (host, metric) not in seen and st["level"] != "ok" and now - _ts(st["updated_at"]) > 15 * 60:
                    self.kb._exec("UPDATE threshold_state SET level='ok', since=?, updated_at=? WHERE host=? AND metric=?", (now_iso, now_iso, host, metric))
        self.runs += 1
        self.last_run = now_iso
        self.last_ms = round((time.perf_counter() - t0) * 1000, 1)
        return sent

    def _step(self, host, metric, env, p, v, lvl, st, now, now_iso) -> list[dict]:
        out = []
        limit = p["crit"] if lvl == "crit" else p.get("warn") if lvl == "warn" else (p.get("warn") if p.get("warn") is not None else p.get("crit"))
        if lvl == "ok":
            if st and st["level"] != "ok":
                if st.get("notified_level") and p.get("notify_resolved") and p.get("id") is not None:
                    out.append(self._notify(p, host, metric, env, v, "resolved", limit))
                self._close_anomaly(host, env, metric)
                self.kb._exec("UPDATE threshold_state SET level='ok', since=?, value=?, limit_value=?, policy_id=?, notified_level='', updated_at=? WHERE host=? AND metric=?",
                              (now_iso, v, limit, p.get("id"), now_iso, host, metric))
            elif st:
                self.kb._exec("UPDATE threshold_state SET value=?, limit_value=?, policy_id=?, updated_at=? WHERE host=? AND metric=?", (v, limit, p.get("id"), now_iso, host, metric))
            return out
        # beyond a level
        if not st or st["level"] == "ok":
            since = now_iso
            demo = int(bool(getattr(self.live, "is_demo_host", None) and self.live.is_demo_host(host)))
            self.kb._exec("INSERT INTO threshold_state (host, metric, level, since, value, limit_value, policy_id, notified_level, notified_at, updated_at, demo) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(host, metric) DO UPDATE SET level=excluded.level, since=excluded.since, value=excluded.value, limit_value=excluded.limit_value, "
                          "policy_id=excluded.policy_id, notified_level='', updated_at=excluded.updated_at, demo=excluded.demo",
                          (host, metric, lvl, since, v, limit, p.get("id"), "", "", now_iso, demo))
            st = {"level": lvl, "since": since, "notified_level": "", "notified_at": ""}
        elif st["level"] != lvl:
            self.kb._exec("UPDATE threshold_state SET level=?, value=?, limit_value=?, policy_id=?, updated_at=? WHERE host=? AND metric=?", (lvl, v, limit, p.get("id"), now_iso, host, metric))
        else:
            self.kb._exec("UPDATE threshold_state SET value=?, limit_value=?, policy_id=?, updated_at=? WHERE host=? AND metric=?", (v, limit, p.get("id"), now_iso, host, metric))
        sustained = (now - _ts(st["since"])) / 60 >= float(p.get("for_min") or 0)
        if not sustained:
            return out
        self._open_anomaly(host, env, metric, v, limit, lvl, p)
        already = st.get("notified_level") or ""
        escalated = already == "warn" and lvl == "crit"
        cooled = not st.get("notified_at") or now - _ts(st["notified_at"]) >= 60 * int(p.get("cooldown_min") or 30)
        if p.get("id") is not None and (not already or escalated or cooled):
            out.append(self._notify(p, host, metric, env, v, lvl, limit))
            self.kb._exec("UPDATE threshold_state SET notified_level=?, notified_at=? WHERE host=? AND metric=?", (lvl, now_iso, host, metric))
        return out

    def _notify(self, p, host, metric, env, v, lvl, limit) -> dict:
        unit = METRICS[metric]["unit"]
        val = f"{v:.0f}{unit}" if unit in ("%", "ms") else f"{v:g} {unit}"
        lim = f"{limit:.0f}{unit}" if limit is not None and unit in ("%", "ms") else f"{limit:g} {unit}" if limit is not None else "-"
        sev = {"crit": "critical", "warn": "medium", "resolved": "low"}[lvl]
        apps = self.apps_of(host)
        tag = f" [{', '.join(apps[:3])}{'…' if len(apps) > 3 else ''}]" if apps else ""
        app_line = f" Uygulama / Application: {', '.join(apps)}." if apps else ""
        if lvl == "resolved":
            title = f"Düzeldi / Resolved · {host}{tag} {metric} {val}"
            body = f"{host} ({env or '-'}): {metric} {val}, back under {lim}. Policy: {p['name']}.{app_line}"
        else:
            title = f"{'KRİTİK / CRITICAL' if lvl == 'crit' else 'Uyarı / Warning'} · {host}{tag} {metric} {val} (≥ {lim})"
            lasting = f" for {p.get('for_min')} min" if p.get("for_min") else ""
            level_name = "critical" if lvl == "crit" else "warning"
            body = (f"{host} ({env or '-'}, pool {self._pool(host) or '-'}): {metric} is {val}, at or above the {level_name} level {lim}{lasting}. "
                    f"Policy: {p['name']} ({p['scope']} {p.get('scope_value') or ''}).{app_line}")
        if self.notifier is None:
            return {"skipped": "no notifier", "title": title}
        rule = {"id": None, "severity": sev, "channels": p.get("channels") or "email", "targets": p.get("targets") or "", "cooldown_min": 0, "team_id": p.get("team_id")}
        try:
            demo = bool(getattr(self.live, "is_demo_host", None) and self.live.is_demo_host(host))
            return self.notifier.dispatch(rule, f"thr:{host}:{metric}:{lvl}", title, body, force=True, **({"demo": True} if demo else {}))
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:120]}", "title": title}

    def _open_anomaly(self, host, env, metric, v, limit, lvl, p) -> None:
        if self.anomalies is None:
            return
        try:
            unit = METRICS[metric]["unit"]
            self.anomalies._upsert("metric", f"threshold:{env or 'unknown'}:{host}:{metric}", env=env or "unknown", host=host, metric=metric,
                                   title=f"{host}: {metric} {v:.0f}{'' if unit == '/min' else unit} ≥ {limit:.0f}" + ("" if unit == "/min" else unit),
                                   detail=f"{metric} is {v:g} {unit} on {host}; {'critical' if lvl == 'crit' else 'warning'} level {limit:g} {unit} (policy: {p['name']})"
                                          + (f"; application: {', '.join(self.apps_of(host))}" if self.apps_of(host) else ""),
                                   observed=v, baseline=float(limit or 0), spread=0.0, score=5.0 if lvl == "crit" else 3.5, series=[v])
        except Exception:  # noqa: BLE001
            pass

    def _close_anomaly(self, host, env, metric) -> None:
        try:
            self.kb._exec("UPDATE anomalies SET status='resolved', cleared_at=?, updated_at=? WHERE key=? AND status IN ('open', 'ack')",
                          (_now(), _now(), f"threshold:{env or 'unknown'}:{host}:{metric}"))
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- views
    def state(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM threshold_state WHERE level != 'ok' ORDER BY level, host")]

    def overview(self, allow=None) -> list[dict]:
        """One row per server: current values, the effective levels per metric and where each came from, and the state."""
        vals = self.values()
        state = {(s["host"], s["metric"]): s for s in self.kb._exec("SELECT * FROM threshold_state")}
        rows = []
        for host, d in sorted(vals.items()):
            if allow is not None and not allow(host):
                continue
            eff = self.effective(host, d.get("env", ""), d.get("pool"))
            ms = {}
            for m in METRICS:
                p = eff.get(m)
                st = state.get((host, m))
                ms[m] = {"value": d.get(m), "warn": p.get("warn") if p else None, "crit": p.get("crit") if p else None,
                         "source": ({"scope": p["scope"], "value": p.get("scope_value") or "", "policy": p.get("id"), "name": p.get("name"), "team_id": p.get("team_id")} if p else None),
                         "level": (st["level"] if st else "ok") if p else None}
            rows.append({"host": host, "env": d.get("env", ""), "pool": d.get("pool", ""), "apps": self.apps_of(host), "metrics": ms,
                         "worst": max((x["level"] or "ok" for x in ms.values()), key=lambda lv: {"ok": 0, "warn": 1, "crit": 2}.get(lv, 0))})
        return rows

    def preview(self, p: dict) -> dict:
        """Which servers a draft policy would cover (and win for), and which of them are beyond its levels right now."""
        row = self._check(p)
        row["team_id"] = p.get("team_id")
        row["id"] = 10 ** 9                            # a draft is the newest
        vals = self.values()
        covered, wins, beyond = [], [], []
        for host, d in sorted(vals.items()):
            if not self.matches(row, host, d.get("env", ""), d.get("pool")):
                continue
            covered.append(host)
            cur = self.effective(host, d.get("env", ""), d.get("pool")).get(row["metric"])
            if cur is None or self._rank(row) >= self._rank(cur):
                wins.append(host)
                v = d.get(row["metric"])
                if v is not None and self._level(row, float(v)) != "ok":
                    beyond.append({"host": host, "value": v, "level": self._level(row, float(v))})
        return {"covered": len(covered), "hosts": covered[:200], "wins": len(wins), "beyond": beyond[:100]}

    # ---------------------------------------------------------------- loop
    def start(self, every: float = 60.0) -> "Thresholds":
        def loop():
            while not self.stop.wait(every):
                try:
                    self.evaluate()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True, name="thresholds").start()
        return self


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        d = datetime.fromisoformat(iso)
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
    except ValueError:
        return 0.0


def webhook_payload(kind: str, severity: str, title: str, body: str) -> dict:
    """The JSON a webhook channel posts: Microsoft Teams (MessageCard), Slack (text + attachment colour) or a plain object."""
    color = {"critical": "F87171", "high": "FB923C", "medium": "FBBF24", "low": "34D399"}.get(severity, "60A5FA")
    if kind == "teams":
        return {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": title[:150], "themeColor": color, "title": title[:250], "text": body}
    if kind == "slack":
        return {"text": title, "attachments": [{"color": f"#{color}", "text": body}]}
    return {"source": "watchover", "severity": severity, "title": title, "body": body, "ts": _now()}


def post_webhook(url: str, kind: str, severity: str, title: str, body: str, secret: str = "", timeout: float = 8.0) -> str:
    import urllib.request
    data = json.dumps(webhook_payload(kind, severity, title, body), ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {secret}"} if secret else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return f"HTTP {r.status}"
