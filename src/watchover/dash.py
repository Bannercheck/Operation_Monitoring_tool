"""Built-in dashboards (the operations home's panel grid): the few aggregates no other endpoint answers in one call.

infra()  - CPU / memory / disk / GPU over a time range from the live metric ring: fleet averages per bar, the top hosts' own lines,
           the newest reading per host with min / avg / max (the "devices by CPU" tables) and the fleet gauges with their levels.
alarms() - one recent-alarms list across the threshold states, the anomalies and the alert log, normalised to crit / warn / info.
pools()  - lines and ERROR+ per pool and bar (the stacked area per pool), folded past `top` into "other".

Every answer is bounded (top-N, item limits) and the router keeps it for a few seconds (TTLCache) so a wall of panels refreshing
together costs one computation."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from . import scenario
from .live import METRICS
from .timerange import step_minutes

UTC = timezone.utc
OTHER = "~other"                                    # the folded remainder of a per-pool chart (the UI names it)
WARN_GAP = 15.0                                     # the fleet gauges' warning band starts this far below the critical level


class TTLCache:
    """A tiny time-boxed memo: key -> (stamp, value), oldest dropped past `size`."""

    def __init__(self, ttl: float = 5.0, size: int = 128):
        self.ttl, self.size = ttl, size
        self._d: dict = {}
        self.lock = threading.Lock()

    def get(self, key, fn):
        now = time.time()
        with self.lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self._d[key] = (now, val)
            while len(self._d) > self.size:
                self._d.pop(next(iter(self._d)))
        return val


def _hset(host) -> set | None:
    """The live store's host argument (None, a name or a tuple) as a set for membership tests."""
    if host is None:
        return None
    return {host} if isinstance(host, str) else set(host)


def fleet_levels(globals_: dict | None = None) -> dict:
    """metric -> {"warn", "crit"}: the critical level from System > Settings (else the scenario default), the warning band below it."""
    g = globals_ or {}
    out = {}
    for m in METRICS:
        crit = g.get(m) if g.get(m) is not None else scenario.METRIC_THRESHOLDS.get(m)
        crit = float(crit) if crit is not None else None
        out[m] = {"warn": max(0.0, crit - WARN_GAP) if crit is not None else None, "crit": crit}
    return out


def _level(v, warn, crit) -> str:
    if v is None:
        return "none"
    if crit is not None and v >= crit:
        return "crit"
    if warn is not None and v >= warn:
        return "warn"
    return "ok"


def infra(live, s: datetime, e: datetime, env: str | None = None, host=None, top: int = 10, series_top: int = 6,
          levels_fn=None, globals_: dict | None = None) -> dict:
    """CPU / memory / disk / GPU in [s, e) for the hosts in scope. `levels_fn(host, metric, env) -> (warn, crit)` gives a server's own
    levels (threshold policies); without it the fleet levels apply. Hosts are ranked by their newest reading."""
    minutes = max(1, round((e - s).total_seconds() / 60))
    step = step_minutes(minutes)
    m0 = int(s.timestamp() // 60)
    nb = max(1, -(-minutes // step))
    hs = _hset(host)
    floor = s - timedelta(minutes=15)                # the ring is in arrival order; a late stamp may sit a little out of place
    rows = []
    with live.lock:
        for r in reversed(live.metrics):
            ts = r[0]
            if ts < floor:
                break
            if ts < s or ts >= e:
                continue
            if env and (r[4] or "unknown") != env:
                continue
            if hs is not None and r[1] not in hs:
                continue
            rows.append(r)
    latest: dict[tuple, tuple] = {}                  # (host, metric) -> (ts, value)
    agg: dict[tuple, list] = {}                      # (host, metric) -> [sum, n, min, max]
    bar: dict[tuple, list] = {}                      # (host, metric, bar) -> [sum, n]
    fbar: dict[tuple, list] = {}                     # (metric, bar) -> [sum, n]
    host_env: dict[str, str] = {}
    for ts, h, m, v, ev in rows:
        host_env.setdefault(h, ev or "unknown")
        cur = latest.get((h, m))
        if cur is None or ts > cur[0]:
            latest[(h, m)] = (ts, v)
        a = agg.get((h, m))
        if a is None:
            agg[(h, m)] = [v, 1, v, v]
        else:
            a[0] += v; a[1] += 1
            if v < a[2]:
                a[2] = v
            if v > a[3]:
                a[3] = v
        b = (int(ts.timestamp() // 60) - m0) // step
        if 0 <= b < nb:
            x = bar.setdefault((h, m, b), [0.0, 0])
            x[0] += v; x[1] += 1
            y = fbar.setdefault((m, b), [0.0, 0])
            y[0] += v; y[1] += 1
    levels = fleet_levels(globals_)
    pool_of = getattr(getattr(live, "rollup", None), "pool_of", None)
    fleet, top_rows, series = {}, {}, {}
    for m in METRICS:
        vals = sorted(((v[1], h) for (h, mm), v in latest.items() if mm == m), key=lambda x: (-x[0], x[1]))
        lv = levels[m]
        if not vals:
            fleet[m] = {"avg": None, "max": None, "worst": None, "hosts": 0, "warn_n": 0, "crit_n": 0, **lv}
            top_rows[m], series[m] = [], {"hosts": [], "rows": []}
            continue
        own = {}
        for v, h in vals[:max(top, series_top)]:
            own[h] = levels_fn(h, m, host_env.get(h, "")) if levels_fn else (lv["warn"], lv["crit"])
        n_warn = n_crit = 0
        for v, h in vals:
            w, c = own.get(h) or (lv["warn"], lv["crit"])
            lvl = _level(v, w, c)
            n_crit += lvl == "crit"; n_warn += lvl == "warn"
        fleet[m] = {"avg": round(sum(v for v, _ in vals) / len(vals), 1), "max": round(vals[0][0], 1), "worst": vals[0][1], "hosts": len(vals),
                    "warn_n": n_warn, "crit_n": n_crit, **lv}
        out = []
        for v, h in vals[:top]:
            a = agg[(h, m)]
            w, c = own[h]
            out.append({"host": h, "env": host_env.get(h, "unknown"), "pool": pool_of(h) if pool_of else "", "value": round(v, 1),
                        "min": round(a[2], 1), "avg": round(a[0] / a[1], 1), "max": round(a[3], 1), "warn": w, "crit": c, "level": _level(v, w, c)})
        top_rows[m] = out
        names = sorted(h for _v, h in vals[:series_top])           # colour follows the host (alphabetical), not its rank
        srows = []
        for i in range(nb):
            row = {"t": (m0 + i * step) * 60}
            for h in names:
                x = bar.get((h, m, i))
                row[h] = round(x[0] / x[1], 1) if x else None
            srows.append(row)
        series[m] = {"hosts": names, "rows": srows}
    trend = []
    for i in range(nb):
        row = {"t": (m0 + i * step) * 60}
        for m in METRICS:
            y = fbar.get((m, i))
            row[m] = round(y[0] / y[1], 1) if y else None
        trend.append(row)
    return {"start": s.isoformat(timespec="seconds"), "end": e.isoformat(timespec="seconds"), "minutes": minutes, "step_min": step,
            "samples": len(rows), "hosts": len(host_env), "fleet": fleet, "trend": trend, "series": series, "top": top_rows}


# ---------------------------------------------------------------- alarms
SEV_OF_ALERT = {"critical": "crit", "crit": "crit", "high": "crit", "medium": "warn", "warn": "warn", "warning": "warn", "low": "info", "info": "info"}


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat(timespec="seconds")
    return str(v or "")


def _in(ts: str, s_iso: str, e_iso: str) -> bool:
    return bool(ts) and s_iso <= ts[:19] <= e_iso


def alarms(s: datetime, e: datetime, states=None, anomalies=None, alerts=None, limit: int = 50) -> dict:
    """Recent alarms in [s, e] from the sources the caller may read (already narrowed to the user's scope): open threshold states
    (still active, so kept while they last), anomalies seen in the range and the alert log of the range. Newest first."""
    s_iso, e_iso = s.astimezone(UTC).isoformat()[:19], e.astimezone(UTC).isoformat()[:19]
    items, active = [], set()
    for r in states or []:
        if r.get("level") not in ("warn", "crit"):
            continue
        since = _iso(r.get("since"))
        if since and since[:19] > e_iso:
            continue
        active.add((r["host"], r["metric"]))
        items.append({"kind": "threshold", "sev": r["level"], "ts": since, "host": r["host"], "metric": r["metric"], "active": True,
                      "title": f"{r['metric']} %{round(float(r.get('value') or 0))}", "detail": f"≥ {r.get('limit_value')}" if r.get("limit_value") is not None else "",
                      "id": f"thr:{r['host']}:{r['metric']}"})
    for a in anomalies or []:
        last, first = _iso(a.get("last_seen")), _iso(a.get("first_seen"))
        if not last or last[:19] < s_iso or (first and first[:19] > e_iso):
            continue
        sc = float(a.get("score") or 0)
        items.append({"kind": "anomaly", "sev": "crit" if sc >= 8 else "warn", "ts": last, "host": a.get("host") or "", "service": a.get("service") or "",
                      "active": a.get("status") in ("open", "ack"), "title": a.get("title") or a.get("kind") or "", "detail": a.get("detail") or "",
                      "id": f"an:{a.get('id')}", "anomaly_id": a.get("id"), "status": a.get("status")})
    for x in alerts or []:
        ts, key = _iso(x.get("ts")), str(x.get("key") or "")
        if not _in(ts, s_iso, e_iso):
            continue
        kind, host = "alert", ""
        if key.startswith("thr:"):
            parts = key.split(":")
            host = parts[1] if len(parts) > 2 else ""
            if len(parts) > 2 and (host, parts[2]) in active:
                continue                                  # the open state above already stands for it
            kind = "threshold"
        elif key.startswith("query:"):
            kind = "log"
        items.append({"kind": kind, "sev": SEV_OF_ALERT.get(str(x.get("severity") or "").lower(), "warn"), "ts": ts, "host": host, "active": False,
                      "title": x.get("title") or key, "detail": (x.get("body") or "")[:200], "id": f"al:{x.get('id')}", "ok": bool(x.get("ok"))})
    items.sort(key=lambda i: i["ts"], reverse=True)
    by_sev = {"crit": 0, "warn": 0, "info": 0}
    by_kind: dict[str, int] = {"threshold": 0, "anomaly": 0, "alert": 0, "log": 0}
    for i in items:
        by_sev[i["sev"]] = by_sev.get(i["sev"], 0) + 1
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {"total": len(items), "active": sum(1 for i in items if i["active"]), "by_sev": by_sev, "by_kind": by_kind, "items": items[:max(1, limit)]}


# ---------------------------------------------------------------- per pool
def pools(ranges, s: datetime, e: datetime, env=None, host=None, top: int = 8) -> dict:
    """Lines and ERROR+ per pool and bar from the per-host series of the range (live counters, stored batches or history alike)."""
    ln = ranges.hostseries(s, e, env, host, "lines", 100_000)
    er = ranges.hostseries(s, e, env, host, "errors", 100_000)
    pool_of = ranges.live.rollup.pool_of
    pmap: dict[str, str] = {}

    def p(h: str) -> str:
        v = pmap.get(h)
        if v is None:
            v = pmap[h] = pool_of(h) or "-"
        return v
    tot: dict[str, list] = {}
    for x in ln.get("hosts", []):
        tot.setdefault(p(x["host"]), [0, 0])[0] += x["total"] or 0
    for x in er.get("hosts", []):
        tot.setdefault(p(x["host"]), [0, 0])[1] += x["total"] or 0
    ranked = [k for k, _v in sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0])) if tot[k][0] or tot[k][1]]
    keep = set(ranked[:max(1, top)])
    fold = lambda pool: pool if pool in keep else OTHER  # noqa: E731

    def by_pool(rows: list) -> dict:
        out = {}
        for row in rows:
            acc = {}
            for h, v in row.items():
                if h == "t" or not v:
                    continue
                k = fold(p(h))
                acc[k] = acc.get(k, 0) + v
            out[row["t"]] = acc
        return out
    lb, eb = by_pool(ln.get("series", [])), by_pool(er.get("series", []))
    names = sorted(keep) + ([OTHER] if len(ranked) > len(keep) else [])   # alphabetical: a pool keeps its colour as the ranking moves
    series, rate = [], []
    for row in ln.get("series", []):
        t = row["t"]; a, b = lb.get(t, {}), eb.get(t, {})
        series.append({"t": t, **{n: a.get(n, 0) for n in names}})
        rate.append({"t": t, **{n: (round(b.get(n, 0) / a[n], 5) if a.get(n) else None) for n in names}})
    other = [0, 0]
    for k in ranked[len(keep):]:
        other[0] += tot[k][0]; other[1] += tot[k][1]
    rows = [{"pool": k, "lines": tot[k][0], "errors": tot[k][1], "error_rate": round(tot[k][1] / tot[k][0], 5) if tot[k][0] else None} for k in ranked[:len(keep)]]
    if len(ranked) > len(keep):
        rows.append({"pool": OTHER, "lines": other[0], "errors": other[1], "error_rate": round(other[1] / other[0], 5) if other[0] else None, "pools": len(ranked) - len(keep)})
    return {"source": ln.get("source"), "step_min": ln.get("step_min"), "pools": rows, "names": names, "series": series, "error_rate": rate}
