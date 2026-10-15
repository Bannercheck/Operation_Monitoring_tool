"""Any time range for the operations screen: "last 15 minutes" or 09:40-10:05 yesterday, from the cheapest exact source.

  last hour      live counters (rollup.py), exact, milliseconds
  up to 48 h     the stored live batches (live_batches); a range with more events than MAX_EVENTS is read as an even
                 sample of its batches and the counts are scaled back up ("sample": 1/k is reported)
  older          the per-minute history (slo_minute): totals, errors, latency and hosts stay exact for 400 days;
                 message groups and raw lines are no longer there

Every answer carries `source` so the screen can say where the numbers come from.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from .models import SEV_RANK
from .rollup import Rollup, _q, host_series_out, pools_of

UTC = timezone.utc
MAX_EVENTS = 250_000
BAR_TARGET = 90                      # timeline bars across the chosen range


def parse_range(last: int | None = None, start: str | None = None, end: str | None = None) -> tuple[datetime, datetime, bool]:
    """(start, end, live). `last` minutes back from now wins; else ISO start / end (end defaults to now)."""
    now = datetime.now(UTC)
    if start and not last:
        s = _dt(start)
        e = _dt(end) if end else now
        if e <= s:
            s, e = e, s
        e = min(e, now)
        return s, max(e, s + timedelta(minutes=1)), e >= now - timedelta(seconds=90)
    last = max(1, min(int(last or 15), 60 * 24 * 400))
    return now - timedelta(minutes=last), now, True


def _dt(v: str) -> datetime:
    v = v.strip().replace("Z", "+00:00")
    if len(v) > 19 and v[-6] == " " and v[-3] == ":":       # "+02:00" arrived unescaped in a query string: the "+" became a space
        v = v[:-6] + "+" + v[-5:]
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def step_minutes(minutes: int) -> int:
    for s in (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440):
        if minutes / s <= BAR_TARGET * 1.4:
            return s
    return 1440


class RangeView:
    def __init__(self, live, history=None):
        self.live, self.history = live, history
        self._cache: dict[tuple, tuple[float, Rollup, dict]] = {}
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- sources
    def _mm(self, s: datetime, e: datetime) -> tuple[int, int]:
        return int(s.timestamp() // 60), int((e.timestamp() - 0.001) // 60)

    def rollup_for(self, s: datetime, e: datetime) -> tuple[Rollup | None, tuple[int, int], dict]:
        """A rollup answering [s, e): the live one, or one built from the stored batches (cached two minutes)."""
        mm = self._mm(s, e)
        live_r = self.live.rollup
        if live_r.covers(mm):
            return live_r, mm, {"source": "live", "sample": 1}
        if not self.live.db:
            return None, mm, {"source": "none", "sample": 1}
        key = mm
        with self.lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < 120:
                return hit[1], mm, hit[2]
        obs, k = self.live.events_between(s, e, MAX_EVENTS)
        if not obs and self.history is not None:
            return None, mm, {"source": "history", "sample": 1}
        r = Rollup(live_r.pool_fn, keep=None)
        r._pool_cache = live_r._pool_cache
        r.add(obs)
        info = {"source": "stored", "sample": k}
        with self.lock:
            self._cache[key] = (time.time(), r, info)
            while len(self._cache) > 6:
                self._cache.pop(next(iter(self._cache)))
        return r, mm, info

    def _hist_cond(self, s: datetime, e: datetime, env=None, host=None) -> tuple[str, list]:
        hosts = None if not host else ([host] if isinstance(host, str) else list(host))
        cond, p = ["minute >= ?", "minute < ?"], [s.strftime("%Y-%m-%dT%H:%M"), (e + timedelta(minutes=1) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M")]
        if env:
            cond.append("env = ?"); p.append(env)
        if hosts:
            cond.append(f"host IN ({', '.join('?' * len(hosts))})"); p += hosts
        return " AND ".join(cond), p

    def _hist_rows(self, s: datetime, e: datetime, env=None, host=None) -> list:
        cond, p = self._hist_cond(s, e, env, host)
        return self.history.kb._exec(f"SELECT * FROM slo_minute WHERE {cond}", tuple(p))

    def _hist_sum(self, s: datetime, e: datetime, env=None, host=None, by: str = "minute") -> list:
        """slo_minute summed by `by` (minute | host | minute, host): total, errors and the latency buckets; the database does the adding."""
        from .history import LAT_COLS
        cond, p = self._hist_cond(s, e, env, host)
        cols = ", ".join(f"SUM({c}) AS {c}" for c in ("total", "errors", *LAT_COLS))
        return self.history.kb._exec(f"SELECT {by}, {cols} FROM slo_minute WHERE {cond} GROUP BY {by}", tuple(p))

    # ---------------------------------------------------------------- answers
    def summary(self, s: datetime, e: datetime, env=None, host=None) -> dict:
        """Timeline + headline numbers for the range, and the same numbers for the range just before it (for the deltas)."""
        minutes = max(1, round((e - s).total_seconds() / 60))
        step = step_minutes(minutes)
        cur = self._figures(s, e, env, host, step)
        prev = self._figures(s - (e - s), s, env, host, None)
        return {"start": s.isoformat(timespec="seconds"), "end": e.isoformat(timespec="seconds"), "minutes": minutes, "step_min": step, **cur,
                "prev": {k: prev.get(k) for k in ("total", "errors", "error_rate", "p95_ms", "per_min", "hosts")}}

    def _figures(self, s, e, env, host, step) -> dict:
        mm = self._mm(s, e)
        live_r = self.live.rollup
        minutes = mm[1] - mm[0] + 1
        if live_r.covers(mm):
            total, errors, hist, _per = live_r.slo_counts(mm, env, host)
            out = {"source": "live", "total": total, "errors": errors, "p95_ms": _q(hist, 0.95)}
            if step:
                out["series"] = live_r.series(mm, env, host, step)
                st = live_r.stats(mm, env, host)
                out["services"] = st["services"]
            out["hosts"] = len([r for r in live_r.host_table(mm, env) if r["events"] and (not host or r["host"] in ((host,) if isinstance(host, str) else host))])
        elif self.history is not None:
            rows = self._hist_rows(s, e, env, host)
            from .history import LAT_COLS, LAT_EDGES
            total = sum(r["total"] for r in rows); errors = sum(r["errors"] for r in rows)
            lat = [sum(r[c] for r in rows) for c in LAT_COLS]
            p95, acc, n = None, 0, sum(lat)
            for i, c in enumerate(lat):
                acc += c
                if n and acc >= 0.95 * n:
                    p95 = float(LAT_EDGES[i] if i < len(LAT_EDGES) else LAT_EDGES[-1] * 2); break
            out = {"source": "history", "total": total, "errors": errors, "p95_ms": p95, "hosts": len({r["host"] for r in rows if r["total"]})}
            if step:
                b: dict[int, list[int]] = {}
                for r in rows:
                    m = int(_dt(r["minute"] + ":00").timestamp() // 60)
                    k = mm[0] + ((m - mm[0]) // step) * step
                    x = b.setdefault(k, [0, 0]); x[0] += r["total"]; x[1] += r["errors"]
                out["series"] = [{"t": k * 60, "total": b.get(k, [0, 0])[0], "errors": b.get(k, [0, 0])[1], "warn": None} for k in range(mm[0], mm[1] + 1, step)]
                svc = {}
                hs = [r for r in self.history.kb._exec("SELECT service, SUM(total) AS t FROM slo_hour_service WHERE hour >= ? AND hour <= ?" + (" AND env = ?" if env else "") + " GROUP BY service",
                                                         (s.strftime("%Y-%m-%dT%H"), e.strftime("%Y-%m-%dT%H"), *([env] if env else [])))]
                for r in hs:
                    svc[r["service"]] = int(r["t"] or 0)
                out["services"] = sorted(svc.items(), key=lambda x: -x[1])[:8] if not host else []
        else:
            out = {"source": "none", "total": 0, "errors": 0, "p95_ms": None, "hosts": 0, "series": [], "services": []}
        out["error_rate"] = round(out["errors"] / out["total"], 5) if out["total"] else None
        out["availability"] = (1 - out["errors"] / out["total"]) if out["total"] else None
        out["per_min"] = round(out["total"] / minutes, 1)
        return out

    def fleet(self, s, e, env=None, allow=None) -> dict:
        mm = self._mm(s, e)
        minutes = mm[1] - mm[0] + 1
        live_r = self.live.rollup
        if live_r.covers(mm):
            rows, source = live_r.host_table(mm, env, allow), "live"
        elif self.history is not None:
            source = "history"
            agg: dict[str, dict] = {}
            for r in self._hist_rows(s, e, env):
                h = r["host"] or "-"
                if allow is not None and not allow(h):
                    continue
                a = agg.setdefault(h, {"host": h, "env": r["env"] or "unknown", "events": 0, "errors": 0, "last": ""})
                a["events"] += r["total"]; a["errors"] += r["errors"]; a["last"] = max(a["last"], r["minute"])
            end_ts = e.timestamp()
            rows = [{**a, "pool": live_r.pool_of(a["host"]), "warnings": 0, "critical": 0, "per_min": round(a["events"] / minutes, 1),
                     "error_rate": round(a["errors"] / a["events"], 4) if a["events"] else 0,
                     "last_seen_s": round(end_ts - _dt(a["last"] + ":00").timestamp()) if a["last"] else None,
                     "silent": bool(a["last"]) and end_ts - _dt(a["last"] + ":00").timestamp() > 360, "service": ""} for a in agg.values()]
        else:
            rows, source = [], "none"
        pools = pools_of(rows, minutes)
        return {"source": source, "pools": pools, "hosts": len(rows), "silent": sum(1 for r in rows if r["silent"]),
                "erroring": sum(1 for r in rows if r["errors"] and r["error_rate"] >= 0.05), "warning": sum(1 for r in rows if r["errors"] and r["error_rate"] < 0.05)}

    def groups(self, s, e, env=None, host=None, min_sev="INFO", q="", service="", limit=100) -> dict:
        r, mm, info = self.rollup_for(s, e)
        if r is None:
            return {"groups": [], "distinct": 0, "total": 0, **info}
        out = r.groups(mm, env, host, min_sev, q, service, limit)
        k = info.get("sample", 1)
        if k > 1:
            for g in out["groups"]:
                g["count"] *= k
            out["total"] *= k
        start_iso = datetime.fromtimestamp(mm[0] * 60, UTC).isoformat(timespec="seconds")
        known = info["source"] == "live" and r.started < mm[0] * 60 - 300    # counters running well before the range began
        for g in out["groups"]:                          # first seen inside this range: a message that did not exist before
            g["new"] = bool(known and g["first"] >= start_iso)
        return {**out, **info}

    def logs(self, s, e, env=None, host=None, min_sev="INFO", q="", service="", limit=200, before: str | None = None) -> dict:
        """Raw lines in the range, newest first, filtered; `before` pages further back."""
        end = min(e, _dt(before)) if before else e
        rank = SEV_RANK.get((min_sev or "INFO").upper(), 0)
        ql = (q or "").lower().strip()
        hs = None if not host else ({host} if isinstance(host, str) else set(host))

        def ok(o) -> bool:
            return (SEV_RANK.get(o.severity, 1) >= rank and (not env or (o.environment or "unknown") == env) and (hs is None or o.host in hs)
                    and (not service or o.service == service) and (not ql or ql in (o.message or "").lower() or ql in (o.host or "").lower()))
        rows, scanned, more = self.live.lines_between(s, end, ok, limit)
        return {"lines": [{"ts": o.timestamp.isoformat(), "severity": o.severity, "host": o.host, "service": o.service, "env": o.environment,
                           "message": o.message, "source": o.source, "agent": o.attributes.get("agent", "")} for o in rows],
                "next": rows[-1].timestamp.isoformat() if more and rows else None, "scanned": scanned}

    # ---------------------------------------------------------------- what is behind a headline number
    def detail(self, s, e, env=None, host=None) -> dict:
        """The detail behind the four headline numbers for the range (same shape as LiveStore.slo_detail, plus services):
        errors by service / host, the message groups that lower availability, per-bar availability and error-budget burn,
        latency per service and the slowest lines, the newest ERROR+ lines."""
        from . import scenario
        minutes = max(1, round((e - s).total_seconds() / 60))
        step = step_minutes(minutes)
        fig = self._figures(s, e, env, host, step)
        r, mm, info = self.rollup_for(s, e)
        k = info.get("sample", 1)
        target = scenario.SLO["availability"]; allowed = 1 - target
        per, cum_e, cum_t = [], 0, 0
        for b in fig.get("series", []):
            cum_e += b["errors"]; cum_t += b["total"]
            per.append({"minute": datetime.fromtimestamp(b["t"], UTC).isoformat(), "total": b["total"], "errors": b["errors"],
                        "availability": (1 - b["errors"] / b["total"]) if b["total"] else None,
                        "budget_left": max(0.0, 1 - (cum_e / cum_t) / allowed) if cum_t and allowed > 0 else None})
        out = {"source": fig["source"], "sample": k, "step_min": step, "total": fig["total"], "errors": fig["errors"], "availability": fig.get("availability"),
               "p95_ms": fig.get("p95_ms"), "allowed_errors": int(allowed * fig["total"]), "slo": scenario.SLO, "sla": scenario.SLA,
               "error_budget": (max(0.0, 1 - (1 - fig["availability"]) / allowed) if fig.get("availability") is not None and allowed > 0 else None),
               "breach_minutes": sum(1 for x in per if x["availability"] is not None and x["availability"] < target), "per_minute": per,
               "by_service": [], "by_host": [], "templates": [], "latency": [], "slowest": [], "recent": [], "services": []}
        if r is not None:
            d = r.detail(mm, env, host)
            out["by_host"] = [(h, n * k) for h, n in d["by_host"]]
            out["latency"], out["slowest"] = d["latency"], d["slowest"]
            out["recent"] = [{"ts": x[0], "severity": x[1], "service": x[2], "host": x[3], "message": x[4]} for x in d["errors_recent"]]
            g = r.groups(mm, env, host, min_sev="ERROR", limit=10)["groups"]
            out["templates"] = [{"template": x["template"], "count": x["count"] * k, "severity": x["severity"], "first": x["first"], "last": x["last"],
                                 "services": [x["service"]], "hosts": [h["host"] for h in x["hosts"]], "sample": x["sample"]} for x in g]
            out["services"] = [{**x, "lines": x["lines"] * k, "errors": x["errors"] * k} for x in r.services(mm, env, host)]
            out["by_service"] = [(x["service"], x["errors"]) for x in out["services"] if x["errors"]]
        elif self.history is not None and not host:
            rows = self.history.kb._exec("SELECT service, SUM(total) AS t, SUM(errors) AS e FROM slo_hour_service WHERE hour >= ? AND hour <= ?" + (" AND env = ?" if env else "") + " GROUP BY service",
                                         (s.strftime("%Y-%m-%dT%H"), e.strftime("%Y-%m-%dT%H"), *([env] if env else [])))
            out["services"] = sorted(({"service": x["service"], "lines": int(x["t"] or 0), "errors": int(x["e"] or 0), "error_rate": round(int(x["e"] or 0) / int(x["t"]), 5) if x["t"] else 0,
                                       "hosts": None, "per_min": round(int(x["t"] or 0) / minutes, 1), "p95_ms": None} for x in rows), key=lambda x: (-x["errors"], -x["lines"]))
            out["by_service"] = [(x["service"], x["errors"]) for x in out["services"] if x["errors"]]
            by_h: dict[str, int] = {}
            for x in self._hist_rows(s, e, env):
                if x["errors"]:
                    by_h[x["host"]] = by_h.get(x["host"], 0) + x["errors"]
            out["by_host"] = sorted(by_h.items(), key=lambda x: -x[1])
        return out

    # ---------------------------------------------------------------- client sessions, latency percentiles, top hosts per bar
    def _step(self, s, e) -> int:
        return step_minutes(max(1, round((e - s).total_seconds() / 60)))

    def sessions(self, s, e, env=None, host=None, q: str = "", limit: int = 200, rum=None) -> dict:
        """Client sessions named by the lines' attributes (rollup.py: pseudonymised at ingest), from the live counters or a rollup of the
        stored batches. Lines and errors of a sampled range are scaled back up; distinct counts are what the sample holds. Older than the
        stored batches only the per-minute history is left and it keeps no sessions: source history, nothing listed. `rum`: the user
        sensors' store, for the web / mobile session figures (the caller passes None when hosts or a pool narrow the scope)."""
        step = self._step(s, e)
        r, mm, info = self.rollup_for(s, e)
        k = info.get("sample", 1)
        out = {"source": info["source"], "step_min": step, "sample": k, "total": 0, "matched": 0, "overflow": 0, "active_series": [], "by_host": [], "sessions": []}
        if r is not None:
            out.update(r.sessions(mm, env, host, step, q, limit))
            if k > 1:
                for x in out["sessions"]:
                    x["lines"] *= k; x["errors"] *= k
                for x in out["by_host"]:
                    x["errors"] *= k
        elif info["source"] == "history" and s >= datetime.now(UTC) - timedelta(hours=float(self.live.retention_h)):
            out["source"] = "stored"                     # inside the stored window, just quiet: not "older than 48 h"
        out["rum"] = rum.range_summary(s, e) if rum is not None else None
        return out

    def latency(self, s, e, env=None, host=None) -> dict:
        """p50 / p90 / p99 per bar from the latency histograms in scope ("<n> ms" in the line, the p95 rule): the live counters or a
        rollup of the stored batches, else the per-minute history (400 days, coarser buckets)."""
        step = self._step(s, e)
        r, mm, info = self.rollup_for(s, e)
        if r is not None:
            series = r.latency_series(mm, env, host, step)
            k = info.get("sample", 1)
            for x in series:
                x["n"] *= k
            return {"source": info["source"], "step_min": step, "sample": k, "series": series}
        if info["source"] != "history":
            return {"source": info["source"], "step_min": step, "series": []}
        from .history import LAT_COLS, LAT_EDGES
        acc: dict[int, list] = {}
        for row in self._hist_sum(s, e, env, host):
            b = (int(_dt(row["minute"] + ":00").timestamp() // 60) - mm[0]) // step
            a = acc.setdefault(b, [0] * len(LAT_COLS))
            for i, c in enumerate(LAT_COLS):
                a[i] += int(row[c] or 0)
        series = []
        for i, t in enumerate(range(mm[0], mm[1] + 1, step)):
            hst = acc.get(i, [0] * len(LAT_COLS))
            series.append({"t": t * 60, "p50": _q(hst, 0.5, LAT_EDGES), "p90": _q(hst, 0.9, LAT_EDGES), "p99": _q(hst, 0.99, LAT_EDGES), "n": sum(hst)})
        return {"source": "history", "step_min": step, "series": series}

    def hostseries(self, s, e, env=None, host=None, metric: str = "lines", top: int = 6) -> dict:
        """The top hosts in scope by `metric` (lines | errors | error_rate | p95 | sessions) over the range and their value per bar, from
        the same sources as latency(); counts of a sampled range are scaled back up. The history keeps no sessions."""
        step = self._step(s, e)
        r, mm, info = self.rollup_for(s, e)
        nb = -(-(mm[1] - mm[0] + 1) // step)
        base = {"source": info["source"], "step_min": step, "metric": metric}
        if r is not None:
            out = r.host_series(mm, metric, env, host, step, top)
            k = info.get("sample", 1)
            if k > 1 and metric in ("lines", "errors"):
                for x in out["hosts"]:
                    x["total"] *= k
                for row in out["series"]:
                    for h in row:
                        if h != "t" and row[h] is not None:
                            row[h] *= k
            return {**base, "sample": k, **out}
        if info["source"] != "history" or metric == "sessions":
            return {**base, "hosts": [], "series": []}
        from .history import LAT_COLS, LAT_EDGES
        tot: dict[str, list] = {}
        for row in self._hist_sum(s, e, env, host, "host"):
            h = row["host"] or "-"
            t, er = int(row["total"] or 0), int(row["errors"] or 0)
            if metric == "p95":
                v = _q([int(row[c] or 0) for c in LAT_COLS], 0.95, LAT_EDGES)
                tot[h] = [v, v]
            elif metric == "lines":
                tot[h] = [t, t]
            elif metric == "errors":
                tot[h] = [er, er]
            else:
                tot[h] = [er, round(er / t, 5) if t else None]
        pick = [h for h, v in sorted(tot.items(), key=lambda x: (-(x[1][0] or 0), x[0])) if v[0]][:max(1, top)]
        val: dict[tuple, object] = {}
        if pick:
            per: dict[tuple, list] = {}
            for row in self._hist_sum(s, e, env, tuple(pick), "minute, host"):
                key = (row["host"] or "-", (int(_dt(row["minute"] + ":00").timestamp() // 60) - mm[0]) // step)
                a = per.setdefault(key, [0, 0] + [0] * len(LAT_COLS))
                a[0] += int(row["total"] or 0); a[1] += int(row["errors"] or 0)
                for i, c in enumerate(LAT_COLS):
                    a[2 + i] += int(row[c] or 0)
            for key, a in per.items():
                val[key] = (a[0] if metric == "lines" else a[1] if metric == "errors" else _q(a[2:], 0.95, LAT_EDGES) if metric == "p95"
                            else (round(a[1] / a[0], 5) if a[0] else None))
        return {**base, **host_series_out({h: tot[h] for h in pick}, val, {h: h for h in pick}, nb, mm[0], step, metric, top)}
