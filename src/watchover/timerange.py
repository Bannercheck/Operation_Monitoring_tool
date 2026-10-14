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
from .rollup import Rollup, _q, pools_of

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

    def _hist_rows(self, s: datetime, e: datetime, env=None, host=None) -> list:
        hosts = None if not host else ([host] if isinstance(host, str) else list(host))
        cond, p = ["minute >= ?", "minute < ?"], [s.strftime("%Y-%m-%dT%H:%M"), (e + timedelta(minutes=1) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M")]
        if env:
            cond.append("env = ?"); p.append(env)
        if hosts:
            cond.append(f"host IN ({', '.join('?' * len(hosts))})"); p += hosts
        return self.history.kb._exec(f"SELECT * FROM slo_minute WHERE {' AND '.join(cond)}", tuple(p))

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
