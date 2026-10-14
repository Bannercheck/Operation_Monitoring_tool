"""Live rollups kept at ingest time, so the operations screen answers from counters instead of re-reading the window.

At 100k+ lines a minute the last 15 minutes are 1.5 million events; reading them (ring + database) on every 5-second refresh
took seconds. Here every event is counted once when it arrives, into per-minute buckets:

  cnt[minute]   host × severity counts          -> totals, errors, per-minute chart, availability, per host / pool / environment
  svc[minute]   host × service counts            -> top services
  lat[minute]   host -> latency histogram         -> p95 (from "<n> ms" in the message, same rule as before)
  g1[minute]    group -> count                    -> deduplicated log groups and their trend
  g5[5 minutes] host × group -> count             -> groups for one host / pool, hosts per group

A group is one message template (variable parts masked) of one service: thousands of identical lines from 30 servers of a
pool become one row with a count, the hosts and the trend. Minute buckets are kept for KEEP minutes; longer windows fall back
to the event window as before. Pools come from the inventory (cluster) or, failing that, the host name without its trailing
number: prd-sap-app-01 .. -20 -> prd-sap-app.
"""
from __future__ import annotations

import re
import threading
import time
from bisect import bisect_left
from collections import Counter, deque
from datetime import UTC, datetime

from .models import SEV_RANK

KEEP = 61                          # minutes of minute buckets (windows up to 60 minutes answer from here)
LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s?ms\b")
BOUNDS = [1, 2, 5, 10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000, 30000, 60000, 120000]
STEM = re.compile(r"^(.*?)[-_.]?\d+[a-z]?$", re.I)
MAX_GROUPS = 60_000


def pool_stem(host: str) -> str:
    """prd-sap-app-01 -> prd-sap-app, web07 -> web, ik-db-a stays ik-db-a; the domain part is dropped."""
    h = (host or "").split(".")[0].lower()
    if not h or h == "-" or re.fullmatch(r"[\d.:]+", host or ""):
        return "-"
    m = STEM.match(h)
    return (m[1] or h) if m else h


def _min(ts: datetime) -> int:
    return int(ts.timestamp() // 60)


def _q(hist: list[int], q: float) -> float | None:
    n = sum(hist)
    if not n:
        return None
    want, acc = q * n, 0
    for i, c in enumerate(hist):
        if c and acc + c >= want:
            lo = BOUNDS[i - 1] if i > 0 else 0
            hi = BOUNDS[i] if i < len(BOUNDS) else BOUNDS[-1] * 2
            return round(lo + (hi - lo) * ((want - acc) / c), 1)
        acc += c
    return float(BOUNDS[-1])


class Rollup:
    def __init__(self, pool_fn=None):
        from .analysis import template_of
        self._tpl = template_of
        self.lock = threading.Lock()
        self.pool_fn = pool_fn                      # host -> pool from the inventory (cluster); None = name stem only
        self._pool_cache: dict[str, tuple[float, str]] = {}
        self.cnt: dict[int, Counter] = {}
        self.svc: dict[int, Counter] = {}
        self.lat: dict[int, dict[tuple, list[int]]] = {}   # minute -> (host, service) -> histogram
        self.slow: dict[int, list] = {}
        self.g1: dict[int, Counter] = {}
        self.g5: dict[int, Counter] = {}
        self.hid: dict[str, int] = {}               # host -> small int (packed keys keep the counters compact)
        self.hosts: list[str] = []
        self.gid: dict[tuple, int] = {}             # (template, service) -> group id
        self.ginfo: list[dict] = []
        self.host_env: dict[str, str] = {}
        self.last_seen: dict[str, float] = {}
        self.errors: deque = deque(maxlen=2000)     # newest ERROR+ events for the detail card
        self.added = 0
        self.add_ms: deque = deque(maxlen=500)

    # ---------------------------------------------------------------- ingest
    def _h(self, host: str) -> int:
        i = self.hid.get(host)
        if i is None:
            i = self.hid[host] = len(self.hosts)
            self.hosts.append(host)
        return i

    def pool_of(self, host: str) -> str:
        hit = self._pool_cache.get(host)
        now = time.time()
        if hit and now - hit[0] < 60:
            return hit[1]
        p = None
        if self.pool_fn is not None:
            try:
                p = self.pool_fn(host)
            except Exception:  # noqa: BLE001
                p = None
        p = p or pool_stem(host)
        self._pool_cache[host] = (now, p)
        return p

    def add(self, events) -> None:
        t0 = time.perf_counter()
        now_min = int(time.time() // 60)
        lo = now_min - KEEP
        with self.lock:
            for o in events:
                ts = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)
                m = _min(ts)
                if m < lo or m > now_min + 2:
                    continue
                host = o.host or "-"
                h = self._h(host)
                sev = o.severity if o.severity in SEV_RANK else "INFO"
                self.cnt.setdefault(m, Counter())[(h, sev)] += 1
                if o.service:
                    self.svc.setdefault(m, Counter())[(h, o.service)] += 1
                if o.environment:
                    self.host_env[host] = o.environment
                self.last_seen[host] = ts.timestamp()
                msg = o.message or ""
                lm = LATENCY_RE.search(msg) if "ms" in msg else None
                if lm:
                    v = float(lm[1])
                    hist = self.lat.setdefault(m, {}).setdefault((host, o.service or "-"), [0] * (len(BOUNDS) + 1))
                    hist[bisect_left(BOUNDS, v)] += 1
                    sl = self.slow.setdefault(m, [])
                    if len(sl) < 10 or v > sl[-1][0]:
                        sl.append((v, ts, host, o.service or "-", msg[:300]))
                        sl.sort(key=lambda x: -x[0])
                        del sl[10:]
                key = (self._tpl(msg), o.service or "-")
                g = self.gid.get(key)
                if g is None:
                    if len(self.ginfo) >= MAX_GROUPS:
                        self._compact_groups(lo)
                    g = self.gid[key] = len(self.ginfo)
                    self.ginfo.append({"template": key[0], "service": key[1], "severity": sev, "sample": msg[:400], "ref": o.ref, "first": ts, "last": ts, "env": o.environment or ""})
                gi = self.ginfo[g]
                if SEV_RANK[sev] > SEV_RANK[gi["severity"]]:
                    gi["severity"] = sev
                if ts >= gi["last"]:
                    gi["last"], gi["sample"], gi["ref"] = ts, msg[:400], o.ref
                self.g1.setdefault(m, Counter())[g] += 1
                self.g5.setdefault(m // 5, Counter())[(h << 20) | g] += 1
                if SEV_RANK[sev] >= 3:
                    self.errors.append((ts, sev, o.service or "-", host, msg[:400]))
            self.added += len(events)
            if self.added % 50 < len(events) or len(self.cnt) > KEEP + 3:
                self._expire(lo)
        self.add_ms.append((time.perf_counter() - t0) * 1000)

    def _expire(self, lo: int) -> None:
        for d in (self.cnt, self.svc, self.lat, self.slow, self.g1):
            for k in [k for k in d if k < lo]:
                del d[k]
        for k in [k for k in self.g5 if k < lo // 5]:
            del self.g5[k]

    def _compact_groups(self, lo: int) -> None:
        """Too many distinct groups (a message the masking cannot tame): forget the ones not seen in the kept window."""
        live = set()
        for c in self.g1.values():
            live.update(c)
        keep = sorted(live)
        remap = {old: new for new, old in enumerate(keep)}
        self.ginfo = [self.ginfo[i] for i in keep]
        self.gid = {k: remap[v] for k, v in self.gid.items() if v in remap}
        for m, c in list(self.g1.items()):
            self.g1[m] = Counter({remap[g]: n for g, n in c.items() if g in remap})
        for b, c in list(self.g5.items()):
            self.g5[b] = Counter({((k >> 20) << 20) | remap[k & 0xFFFFF]: n for k, n in c.items() if (k & 0xFFFFF) in remap})

    # ---------------------------------------------------------------- queries
    def covers(self, window_min: int) -> bool:
        return 0 < window_min <= KEEP - 1

    def _scope(self, env: str | None, host) -> set[int] | None:
        """None = every host; else the host ids in scope (host is a name or a collection of names)."""
        if not env and not host:
            return None
        names = set(self.hosts) if not host else ({host} if isinstance(host, str) else set(host))
        if env:
            names = {n for n in names if (self.host_env.get(n) or "unknown") == env}
        return {self.hid[n] for n in names if n in self.hid}

    def _mins(self, window_min: int) -> range:
        now = int(time.time() // 60)
        return range(now - window_min + 1, now + 1)

    def stats(self, window_min: int, env=None, host=None) -> dict:
        scope = self._scope(env, host)
        mins = self._mins(window_min)
        rows, services, errors, total = [], Counter(), 0, 0
        with self.lock:
            for m in mins:
                per = Counter()
                for (h, sev), n in self.cnt.get(m, {}).items():
                    if scope is None or h in scope:
                        per[sev] += n
                for sev in SEV_RANK:
                    rows.append({"minute": datetime.fromtimestamp(m * 60, UTC), "severity": sev, "events": per.get(sev, 0)})
                total += sum(per.values())
                errors += per.get("ERROR", 0) + per.get("CRITICAL", 0)
                for (h, s), n in self.svc.get(m, {}).items():
                    if scope is None or h in scope:
                        services[s] += n
            cur = sum(n for (h, _s), n in self.cnt.get(mins[-1], {}).items() if scope is None or h in scope)
            prev = sum(n for (h, _s), n in self.cnt.get(mins[-1] - 1, {}).items() if scope is None or h in scope)
            frac = (time.time() % 60) / 60                 # the last 60 seconds: this minute so far + the rest from the previous one
            recent = round(cur + prev * (1 - frac))
        last = None
        seen = [self.last_seen[self.hosts[h]] for h in (scope if scope is not None else range(len(self.hosts))) if self.hosts[h] in self.last_seen]
        if seen:
            last = datetime.fromtimestamp(max(seen), UTC)
        return {"rows": rows, "services": services.most_common(8), "window_total": total, "errors": errors, "per_minute_now": recent, "last": last}

    def slo_counts(self, window_min: int, env=None, host=None) -> tuple[int, int, list[int], list[tuple[int, int, int]]]:
        """(total, errors, latency histogram, per-minute (minute, total, errors))."""
        scope = self._scope(env, host)
        total = errors = 0
        hist = [0] * (len(BOUNDS) + 1)
        per = []
        with self.lock:
            for m in self._mins(window_min):
                t = e = 0
                for (h, sev), n in self.cnt.get(m, {}).items():
                    if scope is None or h in scope:
                        t += n
                        if SEV_RANK[sev] >= 3:
                            e += n
                per.append((m, t, e)); total += t; errors += e
                for (hn, _s), hh in self.lat.get(m, {}).items():
                    if scope is None or self.hid.get(hn) in scope:
                        for i, c in enumerate(hh):
                            hist[i] += c
        return total, errors, hist, per

    def p95(self, hist: list[int]) -> float | None:
        return _q(hist, 0.95)

    def detail(self, window_min: int, env=None, host=None) -> dict:
        """By service / host error counts, latency per host, slowest lines, newest ERROR+ (same scope)."""
        scope = self._scope(env, host)
        mins = self._mins(window_min)
        lo_ts = mins[0] * 60
        by_host, lat_svc, slow = Counter(), {}, []
        with self.lock:
            for m in mins:
                for (h, sev), n in self.cnt.get(m, {}).items():
                    if SEV_RANK[sev] >= 3 and (scope is None or h in scope):
                        by_host[self.hosts[h]] += n
                for (hn, sv), hh in self.lat.get(m, {}).items():
                    if scope is None or self.hid.get(hn) in scope:
                        acc = lat_svc.setdefault(sv, [0] * (len(BOUNDS) + 1))
                        for i, c in enumerate(hh):
                            acc[i] += c
                slow.extend(x for x in self.slow.get(m, []) if scope is None or self.hid.get(x[2]) in scope)
            errs = [e for e in self.errors if e[0].timestamp() >= lo_ts and (scope is None or self.hid.get(e[3]) in scope)]
        by_service = Counter(e[2] for e in errs).most_common()
        slow.sort(key=lambda x: -x[0])
        return {"by_host": by_host.most_common(), "by_service": by_service, "errors_recent": errs[-40:][::-1],
                "latency": sorted(({"service": k, "n": sum(v), "p50": _q(v, .5), "p95": _q(v, .95), "max": _q(v, 1.0)} for k, v in lat_svc.items()), key=lambda r: -(r["p95"] or 0)),
                "slowest": [{"ts": x[1], "service": x[3], "host": x[2], "ms": x[0], "message": x[4]} for x in slow[:10]]}

    def host_counts(self, window_min: int) -> tuple[dict, dict]:
        """(host, env) -> [events, ERROR+] and (host, env) -> {service: ERROR+} over the last window_min minutes (the anomaly scan)."""
        per: dict[tuple[str, str], list[int]] = {}
        err_svc: dict[tuple[str, str], dict] = {}
        with self.lock:
            for m in self._mins(window_min):
                for (h, sev), n in self.cnt.get(m, {}).items():
                    name = self.hosts[h]
                    c = per.setdefault((name, self.host_env.get(name) or "unknown"), [0, 0])
                    c[0] += n
                    if SEV_RANK[sev] >= 3:
                        c[1] += n
            for e in self.errors:
                if e[0].timestamp() >= (self._mins(window_min)[0]) * 60:
                    k = (e[3], self.host_env.get(e[3]) or "unknown")
                    d = err_svc.setdefault(k, {})
                    d[e[2]] = d.get(e[2], 0) + 1
        return per, err_svc

    def host_table(self, window_min: int, env=None, allow=None) -> list[dict]:
        """One row per host: events, warnings, errors, rate, last seen, pool, environment, top service."""
        mins = self._mins(window_min)
        now = time.time()
        acc: dict[int, Counter] = {}
        svc: dict[int, Counter] = {}
        with self.lock:
            for m in mins:
                for (h, sev), n in self.cnt.get(m, {}).items():
                    acc.setdefault(h, Counter())[sev] += n
                for (h, s), n in self.svc.get(m, {}).items():
                    svc.setdefault(h, Counter())[s] += n
            hosts = list(self.hosts)
        out = []
        for h, name in enumerate(hosts):
            if name not in self.last_seen and h not in acc:
                continue
            e = self.host_env.get(name) or "unknown"
            if (env and e != env) or (allow is not None and not allow(name)):
                continue
            c = acc.get(h, Counter())
            tot = sum(c.values())
            err = c.get("ERROR", 0) + c.get("CRITICAL", 0)
            age = now - self.last_seen.get(name, 0)
            out.append({"host": name, "pool": self.pool_of(name), "env": e, "events": tot, "errors": err, "warnings": c.get("WARN", 0), "critical": c.get("CRITICAL", 0),
                        "per_min": round(tot / window_min, 1), "error_rate": round(err / tot, 4) if tot else 0, "last_seen_s": round(age),
                        "silent": age > 300, "service": (svc.get(h) or Counter()).most_common(1)[0][0] if svc.get(h) else ""})
        return out

    def pools(self, window_min: int, env=None, allow=None) -> list[dict]:
        rows = self.host_table(window_min, env, allow)
        by: dict[str, dict] = {}
        for r in rows:
            p = by.setdefault(r["pool"], {"pool": r["pool"], "hosts": [], "events": 0, "errors": 0, "warnings": 0, "critical": 0, "silent": 0, "envs": set()})
            p["hosts"].append(r)
            for k in ("events", "errors", "warnings", "critical"):
                p[k] += r[k]
            p["silent"] += r["silent"]
            p["envs"].add(r["env"])
        out = []
        for p in by.values():
            hs = sorted(p["hosts"], key=lambda r: (-r["errors"], -r["events"]))
            ev = [r["events"] for r in hs if not r["silent"]]
            mean = (sum(ev) / len(ev)) if ev else 0
            out.append({**p, "envs": sorted(p["envs"]), "hosts": hs, "host_count": len(hs), "per_min": round(p["events"] / window_min, 1),
                        "error_rate": round(p["errors"] / p["events"], 4) if p["events"] else 0, "worst": hs[0]["host"] if hs and hs[0]["errors"] else None,
                        # a host far off its pool's volume (5x more or 5x less than the pool mean) is worth a look
                        "outliers": [r["host"] for r in hs if mean and len(ev) >= 3 and not r["silent"] and (r["events"] > 5 * mean or r["events"] < mean / 5)]})
        return sorted(out, key=lambda p: (-p["errors"], -p["events"]))

    def groups(self, window_min: int, env=None, host=None, min_sev: str = "INFO", q: str = "", service: str = "", limit: int = 100) -> dict:
        """Deduplicated log groups in scope: template × service with count, severity, hosts, pools, first / last, trend."""
        scope = self._scope(env, host)
        mins = self._mins(window_min)
        rank = SEV_RANK.get(min_sev.upper(), 0)
        ql = q.lower().strip()
        counts: Counter = Counter()
        hosts_of: dict[int, Counter] = {}
        trend: dict[int, list[int]] = {}
        with self.lock:
            info = self.ginfo

            def ok(g: int) -> bool:
                gi = info[g]
                return SEV_RANK[gi["severity"]] >= rank and (not service or gi["service"] == service) and (not ql or ql in gi["template"] or ql in gi["service"].lower())
            b_lo, b_hi = mins[0] // 5, mins[-1] // 5
            for b in range(b_lo, b_hi + 1):
                for k, n in self.g5.get(b, {}).items():
                    h, g = k >> 20, k & 0xFFFFF
                    if (scope is None or h in scope) and ok(g):
                        hosts_of.setdefault(g, Counter())[h] += n
                        if scope is not None:
                            counts[g] += n
            if scope is None:              # exact per-minute counts when nothing narrows the hosts
                for i, m in enumerate(mins):
                    for g, n in self.g1.get(m, {}).items():
                        if g in hosts_of:
                            counts[g] += n
                            trend.setdefault(g, [0] * len(mins))[i] += n
            else:                          # narrowed to some hosts: the trend is in 5-minute steps
                for b in range(b_lo, b_hi + 1):
                    for k, n in self.g5.get(b, {}).items():
                        g = k & 0xFFFFF
                        if (k >> 20) in scope and g in hosts_of:
                            trend.setdefault(g, [0] * (b_hi - b_lo + 1))[b - b_lo] += n
            top = [g for g, _ in counts.most_common()]
            total = sum(counts.values())
            rows = []
            for g in top[:limit]:
                gi = info[g]
                hs = hosts_of.get(g, Counter())
                pools = Counter()
                for h, n in hs.items():
                    pools[self.pool_of(self.hosts[h])] += n
                rows.append({"id": g, "template": gi["template"], "service": gi["service"], "severity": gi["severity"], "count": counts[g], "sample": gi["sample"], "ref": gi["ref"],
                             "first": gi["first"].isoformat(timespec="seconds"), "last": gi["last"].isoformat(timespec="seconds"), "host_count": len(hs),
                             "hosts": [{"host": self.hosts[h], "count": n} for h, n in hs.most_common(30)], "pools": [{"pool": p, "count": n} for p, n in pools.most_common(10)],
                             "trend": trend.get(g, [])})
        return {"groups": rows, "distinct": len(counts), "total": total, "window": window_min}

    def stats_internal(self) -> dict:
        a = sorted(self.add_ms)
        return {"hosts": len(self.hosts), "groups": len(self.ginfo), "minutes": len(self.cnt), "added": self.added,
                "add_ms_p95": round(a[int(len(a) * 0.95) - 1], 2) if len(a) >= 20 else None}
