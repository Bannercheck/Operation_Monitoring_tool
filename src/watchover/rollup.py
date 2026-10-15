"""Live rollups kept at ingest time, so the operations screen answers from counters instead of re-reading the window.

At 100k+ lines a minute the last 15 minutes are 1.5 million events; reading them (ring + database) on every 5-second refresh
took seconds. Here every event is counted once when it arrives, into per-minute buckets:

  cnt[minute]   host × severity counts          -> totals, errors, per-minute chart, availability, per host / pool / environment
  svc[minute]   host × service counts            -> top services
  lat[minute]   host -> latency histogram         -> p95 (from "<n> ms" in the message, same rule as before)
  g1[minute]    group -> count                    -> deduplicated log groups and their trend
  g5[5 minutes] host × group -> count             -> groups for one host / pool, hosts per group
  ses[5 minutes] host -> session -> record         -> client sessions: lines, errors, first / last, active minutes, services

A session is what a line's attributes name: a session id (app / SSO / the user sensors' live copy), else a user (+ client
address), else a client address alone. Every identifier is pseudonymised (privacy.pseudonym) before it is kept, so the raw id,
user name or IP never sits in the counters, in the stored batches or in an answer (the one exception is the user sensors' own
random session token, kept for the link to that session's timeline, which already uses it as its address). At most SES_CAP sessions per host per
5-minute bucket (and SES_BUCKET_CAP over all hosts) are kept; the rest are only counted (overflow), so a scanner or a bot cannot grow
memory without bound.

A group is one message template (variable parts masked) of one service: thousands of identical lines from 30 servers of a
pool become one row with a count, the hosts and the trend. Minute buckets are kept for KEEP minutes; longer windows fall back
to the event window as before. Pools come from the inventory (cluster) or, failing that, the host name without its trailing
number: prd-sap-app-01 .. -20 -> prd-sap-app.
"""
from __future__ import annotations

import heapq
import re
import threading
import time
from bisect import bisect_left
from collections import Counter, deque
from datetime import datetime, timezone

from . import privacy as _pv
from .models import SEV_RANK
from .privacy import pseudonym

UTC = timezone.utc

KEEP = 61                          # minutes of minute buckets (windows up to 60 minutes answer from here)
LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s?ms\b")
BOUNDS = [1, 2, 5, 10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000, 30000, 60000, 120000]
STEM = re.compile(r"^(.*?)[-_.]?\d+[a-z]?$", re.I)
MAX_GROUPS = 60_000
SES_CAP = 5000                     # sessions kept per host per 5-minute bucket
SES_BUCKET_CAP = 50_000            # sessions kept per 5-minute bucket over all hosts (~0.5 KB each): a cookie-less bot getting a new session
                                   # id per request on 300 hosts would otherwise keep 300 x 5000 records per bucket (gigabytes over the hour)
OVER_CAP = 50_000                  # distinct overflow sessions remembered (as hashes) per bucket over all hosts; beyond that the count is a floor
TRACED = 32                        # record mask bit: a line of the session had a duration or a trace id (it is a span in the trace store)
# attribute name (lower case) -> role. s session id, S SSO session (session_state), k SSO client id, u user, c client address,
# a "client" when it holds an address (the access-log parser's name for the remote address; Keycloak uses it for the application),
# x forwarded-for list (first address), r / R the user sensors' session and pseudonymous user (rum.* on their live copy),
# t a trace / request id or a duration, P a session already resolved and pseudonymised (stored batches)
SES_ROLES = {**dict.fromkeys(("session", "session_id", "sessionid", "jsessionid", "session.id", "sid", "phpsessid", "asp.net_sessionid"), "s"),
             "session_state": "S", "clientid": "k", "client_id": "k",
             **dict.fromkeys(("user", "user_id", "userid", "username", "user.name", "uid", "cs-username", "usr"), "u"),
             **dict.fromkeys(("client_ip", "clientip", "remote_addr", "remote_ip", "c-ip", "src_ip", "source_ip", "client.ip"), "c"),
             "client": "a", "x_forwarded_for": "x", "x-forwarded-for": "x", "rum.session": "r", "rum.user_ref": "R", "_ses": "P",
             **dict.fromkeys(("traceparent", "trace_id", "traceid", "trace.id", "request_id", "requestid", "x-request-id", "correlation_id", "duration_ms",
                              "elapsed_ms", "latency_ms", "response_time_ms", "time-taken", "time_taken", "request_time", "duration"), "t")}
_KROLE: dict[str, str] = {}        # attribute name as sent -> role ("" none): one lower() per distinct name, not per line
_SC: dict[int, tuple] = {}         # hash(raw values) -> session_of answer
_SC_KEY: list = [None]             # the pseudonym key _SC was filled with (a new key, as in tests, empties it)
RUM_ID = re.compile(r"[^\w\-]")
IP_LIKE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f]*:[0-9a-f:.]+)$", re.I)
PSEUDO = re.compile(r"^u_[0-9a-f]{16}$")


def session_of(a: dict) -> tuple | None:
    """(id, kind, user, client, RUM session, traced) named by one line's attributes, every identifier pseudonymised; None when the
    line names no session, user or client address. kind: app | sso | client | rum. Hot path (every ingested line): the role of an
    attribute name is cached, and so is the answer for the same raw values (keyed by their hash, the values themselves are not kept)."""
    sess = sst = user = cli = rum = ru = None
    sso = traced = False
    for k, v in a.items():
        r = _KROLE.get(k)
        if r is None:
            r = SES_ROLES.get(str(k).lower(), "")
            if len(_KROLE) < 20_000:
                _KROLE[k] = r
        if not r or v is None or v == "" or v == "-":
            continue
        if r == "s":
            if sess is None:
                sess = v if v.__class__ is str else str(v)
        elif r == "u":
            if user is None:
                user = v if v.__class__ is str else str(v)
        elif r == "c":
            if cli is None:
                cli = v if v.__class__ is str else str(v)
        elif r == "t":
            traced = True
        elif r == "k":
            sso = True
        elif r == "S":
            sso = True
            sst = str(v)
        elif r == "a":
            if cli is None and IP_LIKE.match(str(v)):
                cli = str(v)
        elif r == "x":
            if cli is None:
                cli = str(v).split(",")[0].strip() or None
        elif r == "r":
            rum = str(v)
        elif r == "R":
            ru = str(v)
        elif r == "P":                   # only the tuple _unpack puts back: a log line's own "_ses" (a JSON list) must not pass raw values off as pseudonyms
            if v.__class__ is tuple and len(v) == 6:
                return v
    if sess is None:
        sess = sst
    if sess is None and user is None and cli is None and rum is None:
        return None
    if _SC_KEY[0] is not _pv._KEY:
        _SC.clear(); _SC_KEY[0] = _pv._KEY
    ck = hash((sess, user, cli, rum, ru, sso, traced))
    out = _SC.get(ck)
    if out is None:
        out = _session_info(sess, user, cli, rum, ru, sso, traced)
        if len(_SC) > 100_000:
            _SC.clear()
        _SC[ck] = out
    return out or None


def _session_info(sess, user, cli, rum, ru, sso, traced) -> tuple:
    up = pseudonym(user, "u") if user is not None else ""
    cp = pseudonym(cli, "c") if cli is not None else ""
    if rum is not None:
        rid = RUM_ID.sub("", rum)[:64]
        if rid:
            ru = ru or ""
            return pseudonym(rid, "s"), "rum", ru if PSEUDO.match(ru) else (pseudonym(ru, "u") if ru else up), "", rid, traced
    if sess is not None:
        sid = pseudonym(sess[:128], "s")
        if sid:
            return sid, "sso" if sso else "app", up, cp, "", traced
    if up:
        return pseudonym(f"{user}|{cli or ''}", "s"), "client", up, cp, "", traced
    if cp:
        return pseudonym(f"|{cli}", "s"), "client", "", cp, "", traced
    return ()


def session_token(stored_id: str) -> str:
    """The pseudonym the operations screen shows for a session id kept raw elsewhere (trace store: "<id>" or "sso:<id>")."""
    v = stored_id[4:] if stored_id.startswith("sso:") else stored_id
    return pseudonym(v[:128], "s")


def pool_stem(host: str) -> str:
    """prd-sap-app-01 -> prd-sap-app, web07 -> web, ik-db-a stays ik-db-a; the domain part is dropped."""
    h = (host or "").split(".")[0].lower()
    if not h or h == "-" or re.fullmatch(r"[\d.:]+", host or ""):
        return "-"
    m = STEM.match(h)
    return (m[1] or h) if m else h


def _min(ts: datetime) -> int:
    return int(ts.timestamp() // 60)


def _q(hist: list[int], q: float, bounds=BOUNDS) -> float | None:
    """Quantile of a histogram whose bucket i holds (bounds[i-1], bounds[i]], interpolated inside the bucket."""
    n = sum(hist)
    if not n:
        return None
    want, acc = q * n, 0
    for i, c in enumerate(hist):
        if c and acc + c >= want:
            lo = bounds[i - 1] if i > 0 else 0
            hi = bounds[i] if i < len(bounds) else bounds[-1] * 2
            return round(lo + (hi - lo) * ((want - acc) / c), 1)
        acc += c
    return float(bounds[-1])


class Rollup:
    def __init__(self, pool_fn=None, keep: int | None = KEEP):
        from .analysis import template_of
        self.keep = keep                            # minutes kept (None: a one-off rollup over a past range, nothing expires)
        self._tpl = template_of
        self.lock = threading.Lock()
        self.pool_fn = pool_fn                      # host -> pool from the inventory (cluster); None = name stem only
        self._pool_cache: dict[str, tuple[float, str]] = {}
        self.cnt: dict[int, Counter] = {}
        self.svc: dict[int, Counter] = {}
        self.svc_err: dict[int, Counter] = {}       # minute -> (host, service) -> ERROR+ lines
        self.lat: dict[int, dict[tuple, list[int]]] = {}   # minute -> (host, service) -> histogram
        self.slow: dict[int, list] = {}
        self.g1: dict[int, Counter] = {}
        self.g5: dict[int, Counter] = {}
        self.ses: dict[int, dict[int, dict]] = {}   # 5-minute bucket -> host id -> session id -> [lines, errors, first, last, minute mask, service(s), info]
        self.ses_over: dict[int, dict[int, set]] = {}   # 5-minute bucket -> host id -> hashes of the sessions beyond SES_CAP
        self.ses_n: dict[int, list] = {}            # 5-minute bucket -> [records kept, overflow hashes kept] (the per-bucket caps)
        self.hid: dict[str, int] = {}               # host -> small int (packed keys keep the counters compact)
        self.hosts: list[str] = []
        self.gid: dict[tuple, int] = {}             # (template, service) -> group id
        self.ginfo: list[dict] = []
        self.host_env: dict[str, str] = {}
        self.last_seen: dict[str, float] = {}
        self.errors: deque = deque(maxlen=2000)     # newest ERROR+ events for the detail card
        self.added = 0
        self.started = time.time()                 # "new" message groups only mean something once the counters predate the range
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
        """Count a batch. Hot path (100k+ lines a minute): locals instead of attribute lookups, and a bucket is created only when it
        is missing (setdefault(m, Counter()) built a throw-away Counter for every line)."""
        t0 = time.perf_counter()
        now_min = int(time.time() // 60)
        lo = now_min - (self.keep or 10 ** 9)
        cnt, svcc, svce, lat, g1, g5, ses, hid, sevr = self.cnt, self.svc, self.svc_err, self.lat, self.g1, self.g5, self.ses, self.hid, SEV_RANK
        pb = ph = pd = None                                # last bucket and host -> its session dict: a batch is mostly one host
        with self.lock:
            for o in events:
                ts = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)
                m = _min(ts)
                if self.keep and (m < lo or m > now_min + 2):
                    continue
                host = o.host or "-"
                h = hid.get(host)
                if h is None:
                    h = self._h(host)
                sev = o.severity if o.severity in sevr else "INFO"
                bad = sevr[sev] >= 3
                service = o.service or "-"
                c = cnt.get(m)
                if c is None:
                    c = cnt[m] = Counter()
                c[(h, sev)] += 1
                if o.service:
                    c = svcc.get(m)
                    if c is None:
                        c = svcc[m] = Counter()
                    c[(h, service)] += 1
                    if bad:
                        c = svce.get(m)
                        if c is None:
                            c = svce[m] = Counter()
                        c[(h, service)] += 1
                if o.environment:
                    self.host_env[host] = o.environment
                tsf = self.last_seen[host] = ts.timestamp()
                msg = o.message or ""
                lm = LATENCY_RE.search(msg) if "ms" in msg else None
                a = o.attributes
                ss = session_of(a) if a else None
                if ss is not None:
                    b = m // 5
                    if b != pb or h != ph:
                        hd = ses.get(b)
                        if hd is None:
                            hd = ses[b] = {}
                        pd = hd.get(h)
                        if pd is None:
                            pd = hd[h] = {}
                        pb, ph = b, h
                    rec = pd.get(ss[0])
                    bit = (1 << (m - b * 5)) | (TRACED if (lm is not None or ss[5]) else 0)
                    if rec is not None:
                        rec[0] += 1
                        if bad:
                            rec[1] += 1
                        if tsf > rec[3]:
                            rec[3] = tsf
                        elif tsf < rec[2]:
                            rec[2] = tsf
                        rec[4] |= bit
                        if rec[5] != service:
                            if rec[5].__class__ is str:
                                rec[5] = {rec[5], service}
                            else:
                                rec[5].add(service)
                        if ss[2] and not rec[6][2]:
                            rec[6] = ss
                    else:
                        bn = self.ses_n.get(b)
                        if bn is None:
                            bn = self.ses_n[b] = [0, 0]
                        if len(pd) < SES_CAP and bn[0] < SES_BUCKET_CAP:
                            pd[ss[0]] = [1, 1 if bad else 0, tsf, tsf, bit, service, ss]
                            bn[0] += 1
                        elif bn[1] < OVER_CAP:
                            ov = self.ses_over.setdefault(b, {}).setdefault(h, set())
                            if hash(ss[0]) not in ov:
                                ov.add(hash(ss[0])); bn[1] += 1
                if lm:
                    v = float(lm[1])
                    lt = lat.get(m)
                    if lt is None:
                        lt = lat[m] = {}
                    hist = lt.get((host, service))
                    if hist is None:
                        hist = lt[(host, service)] = [0] * (len(BOUNDS) + 1)
                    hist[bisect_left(BOUNDS, v)] += 1
                    sl = self.slow.get(m)
                    if sl is None:
                        sl = self.slow[m] = []
                    if len(sl) < 10 or v > sl[-1][0]:
                        sl.append((v, ts, host, service, msg[:300]))
                        sl.sort(key=lambda x: -x[0])
                        del sl[10:]
                key = (self._tpl(msg), service)
                g = self.gid.get(key)
                if g is None:
                    if len(self.ginfo) >= MAX_GROUPS:
                        self._compact_groups(lo)
                    g = self.gid[key] = len(self.ginfo)
                    self.ginfo.append({"template": key[0], "service": key[1], "severity": sev, "sample": msg[:400], "ref": o.ref, "first": ts, "last": ts, "env": o.environment or ""})
                gi = self.ginfo[g]
                if sevr[sev] > sevr[gi["severity"]]:
                    gi["severity"] = sev
                if ts >= gi["last"]:
                    gi["last"], gi["sample"], gi["ref"] = ts, msg[:400], o.ref
                c = g1.get(m)
                if c is None:
                    c = g1[m] = Counter()
                c[g] += 1
                c = g5.get(m // 5)
                if c is None:
                    c = g5[m // 5] = Counter()
                c[(h << 20) | g] += 1
                if bad:
                    self.errors.append((ts, sev, service, host, msg[:400]))
            self.added += len(events)
            if self.keep and (self.added % 50 < len(events) or len(self.cnt) > self.keep + 3):
                self._expire(lo)
        self.add_ms.append((time.perf_counter() - t0) * 1000)

    def _expire(self, lo: int) -> None:
        for d in (self.cnt, self.svc, self.svc_err, self.lat, self.slow, self.g1):
            for k in [k for k in d if k < lo]:
                del d[k]
        for d in (self.g5, self.ses, self.ses_over, self.ses_n):
            for k in [k for k in d if k < lo // 5]:
                del d[k]

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
    def covers(self, window) -> bool:
        """window: minutes back from now, or (first, last) epoch minutes."""
        if isinstance(window, tuple):
            return self.keep is None or window[0] >= int(time.time() // 60) - self.keep + 1
        return 0 < window <= (self.keep or 10 ** 9) - 1

    def _scope(self, env: str | None, host) -> set[int] | None:
        """None = every host; else the host ids in scope (host is a name or a collection of names)."""
        if not env and not host:
            return None
        names = set(self.hosts) if not host else ({host} if isinstance(host, str) else set(host))
        if env:
            names = {n for n in names if (self.host_env.get(n) or "unknown") == env}
        return {self.hid[n] for n in names if n in self.hid}

    def _mins(self, window) -> range:
        if isinstance(window, tuple):
            return range(window[0], window[1] + 1)
        now = int(time.time() // 60)
        return range(now - window + 1, now + 1)

    def series(self, window, env=None, host=None, step: int = 1) -> list[dict]:
        """Timeline buckets of `step` minutes: total, errors (ERROR+), warnings."""
        scope = self._scope(env, host)
        mins = self._mins(window)
        out = []
        with self.lock:
            for b0 in range(mins[0], mins[-1] + 1, step):
                t = e = w = 0
                for m in range(b0, min(b0 + step, mins[-1] + 1)):
                    for (h, sev), n in self.cnt.get(m, {}).items():
                        if scope is None or h in scope:
                            t += n
                            if SEV_RANK[sev] >= 3:
                                e += n
                            elif sev == "WARN":
                                w += n
                out.append({"t": b0 * 60, "total": t, "errors": e, "warn": w})
        return out

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

    def services(self, window, env=None, host=None) -> list[dict]:
        """One row per service in scope: lines, ERROR+, error rate, hosts, lines per minute, p95 (from its latency histograms)."""
        scope = self._scope(env, host)
        mins = self._mins(window)
        tot: Counter = Counter(); err: Counter = Counter(); hosts: dict[str, set] = {}; lat: dict[str, list[int]] = {}
        with self.lock:
            for m in mins:
                for (h, s), n in self.svc.get(m, {}).items():
                    if scope is None or h in scope:
                        tot[s] += n; hosts.setdefault(s, set()).add(h)
                for (h, s), n in self.svc_err.get(m, {}).items():
                    if scope is None or h in scope:
                        err[s] += n
                for (hn, sv), hh in self.lat.get(m, {}).items():
                    if scope is None or self.hid.get(hn) in scope:
                        acc = lat.setdefault(sv, [0] * (len(BOUNDS) + 1))
                        for i, c in enumerate(hh):
                            acc[i] += c
        return sorted(({"service": s, "lines": n, "errors": err.get(s, 0), "error_rate": round(err.get(s, 0) / n, 5) if n else 0, "hosts": len(hosts.get(s, ())),
                        "per_min": round(n / len(mins), 1), "p95_ms": _q(lat[s], 0.95) if s in lat else None} for s, n in tot.items()), key=lambda r: (-r["errors"], -r["lines"]))

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
        now = min(time.time(), (mins[-1] + 1) * 60)       # a past range: "silent" means silent at its end
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
            if not tot and isinstance(window_min, tuple) and self.keep:
                continue                                   # a past range: only the hosts that sent something in it
            out.append({"host": name, "pool": self.pool_of(name), "env": e, "events": tot, "errors": err, "warnings": c.get("WARN", 0), "critical": c.get("CRITICAL", 0),
                        "per_min": round(tot / len(mins), 1), "error_rate": round(err / tot, 4) if tot else 0, "last_seen_s": round(age),
                        "silent": age > 300, "service": (svc.get(h) or Counter()).most_common(1)[0][0] if svc.get(h) else ""})
        return out

    def pools(self, window_min, env=None, allow=None) -> list[dict]:
        return pools_of(self.host_table(window_min, env, allow), len(self._mins(window_min)))

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
            if scope is None:              # exact per-minute counts when nothing narrows the hosts (trend: at most 60 points)
                step = -(-len(mins) // 60)
                width = -(-len(mins) // step)
                for i, m in enumerate(mins):
                    for g, n in self.g1.get(m, {}).items():
                        if g in hosts_of:
                            counts[g] += n
                            trend.setdefault(g, [0] * width)[i // step] += n
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

    # ---------------------------------------------------------------- sessions, latency percentiles and per-host series (bars of `step` minutes)
    def _ses_scan(self, mins, scope):
        """(bucket, host id, record, minute mask inside the range) for every session record touching the range; under the lock."""
        lo, hi = mins[0], mins[-1]
        for b in range(lo // 5, hi // 5 + 1):
            hd = self.ses.get(b)
            if not hd:
                continue
            bm = sum(1 << i for i in range(5) if lo <= b * 5 + i <= hi)
            for h, d in hd.items():
                if scope is None or h in scope:
                    for rec in d.values():
                        mk = rec[4] & bm
                        if mk:
                            yield b, h, rec, mk

    def sessions(self, window, env=None, host=None, step: int = 1, q: str = "", limit: int = 200) -> dict:
        """Client sessions in scope: distinct total, distinct active per bar, per host (sessions, users, ERROR+ lines) and the sessions
        themselves, most recent first (errors first on ties). Lines are counted per 5-minute bucket: a session active at the edge of
        the range brings its whole bucket. Hundreds of thousands of records at 300 hosts: under the lock only one list per session
        (a host / service stays a plain value until a second one shows up, the active minutes are one int), so ingest is not held up
        by the allocator and the garbage collector; the bars and the rows (names, sorting) are built after it, the rows only for `limit`."""
        scope = self._scope(env, host)
        mins = self._mins(window)
        m0 = mins[0]
        agg: dict[str, list] = {}                  # sid -> [lines, errors, first, last, host id | {ids}, service | {services}, info, traced, minute mask]
        per_host: dict[int, list] = {}
        over = 0
        with self.lock:
            for b, h, rec, mk in self._ses_scan(mins, scope):
                info = rec[6]
                sid = info[0]
                sh = b * 5 - m0
                mk = mk << sh if sh >= 0 else mk >> -sh
                g = agg.get(sid)
                sv = rec[5]
                if g is None:
                    agg[sid] = [rec[0], rec[1], rec[2], rec[3], h, sv if sv.__class__ is str else set(sv), info, rec[4] & TRACED, mk]
                else:
                    g[0] += rec[0]; g[1] += rec[1]
                    if rec[2] < g[2]:
                        g[2] = rec[2]
                    if rec[3] > g[3]:
                        g[3] = rec[3]
                    if g[4] != h:
                        if g[4].__class__ is int:
                            g[4] = {g[4], h}
                        else:
                            g[4].add(h)
                    if g[5] != sv:
                        if g[5].__class__ is str:
                            g[5] = {g[5]}
                        if sv.__class__ is str:
                            g[5].add(sv)
                        else:
                            g[5].update(sv)
                    if info[2] and not g[6][2]:
                        g[6] = info
                    g[7] |= rec[4] & TRACED
                    g[8] |= mk
                ph = per_host.get(h)
                if ph is None:
                    ph = per_host[h] = [set(), set(), 0]
                ph[0].add(sid); ph[2] += rec[1]
                if info[2]:
                    ph[1].add(info[2])
            for b in range(m0 // 5, mins[-1] // 5 + 1):
                for h, ov in self.ses_over.get(b, {}).items():
                    if scope is None or h in scope:
                        over += len(ov)
            names = list(self.hosts)
        bars = [0] * -(-len(mins) // step)
        for g in agg.values():                      # distinct sessions per bar: each session once per bar it was active in
            m, last = g[8], -1
            while m:
                lb = m & -m
                i = (lb.bit_length() - 1) // step
                if i != last:
                    bars[i] += 1; last = i
                m ^= lb
        hosts_of = lambda g: sorted(names[x] for x in g[4]) if g[4].__class__ is set else [names[g[4]]]   # noqa: E731
        svcs_of = lambda g: [g[5]] if g[5].__class__ is str else (sorted(g[5] - {"-"}) or sorted(g[5]))    # noqa: E731
        order = lambda g: (-int(g[3]), -g[1])     # noqa: E731
        ql = (q or "").lower().strip()
        if ql:
            hit = [g for g in agg.values() if any(ql in x for x in g[6][:4]) or any(ql in x.lower() for x in hosts_of(g) + svcs_of(g))]
            matched = len(hit)
            top = sorted(hit, key=order)[:max(0, limit)]
        else:
            matched = len(agg)
            top = heapq.nsmallest(max(0, limit), agg.values(), key=order)
        iso = lambda t: datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")   # noqa: E731
        return {"total": len(agg), "matched": matched, "overflow": over,
                "active_series": [{"t": (m0 + i * step) * 60, "n": n} for i, n in enumerate(bars)],
                "by_host": sorted(({"host": names[h], "sessions": len(p[0]), "users": len(p[1]), "errors": p[2]} for h, p in per_host.items()),
                                  key=lambda r: (-r["sessions"], -r["errors"], r["host"])),
                "sessions": [{"id": g[6][0], "kind": g[6][1], "user": g[6][2], "client": g[6][3], "hosts": hosts_of(g)[:20], "services": svcs_of(g)[:20],
                              "first": iso(g[2]), "last": iso(g[3]), "duration_s": int(round(g[3] - g[2])), "lines": g[0], "errors": g[1],
                              "link": session_link(g[6], g[7])} for g in top]}

    def latency_series(self, window, env=None, host=None, step: int = 1) -> list[dict]:
        """p50 / p90 / p99 (ms) and the number of duration samples per bar; None where a bar has no sample."""
        scope = self._scope(env, host)
        mins = self._mins(window)
        out = []
        with self.lock:
            for b0 in range(mins[0], mins[-1] + 1, step):
                hist = [0] * (len(BOUNDS) + 1)
                for m in range(b0, min(b0 + step, mins[-1] + 1)):
                    for (hn, _s), hh in self.lat.get(m, {}).items():
                        if scope is None or self.hid.get(hn) in scope:
                            for i, c in enumerate(hh):
                                if c:
                                    hist[i] += c
                out.append({"t": b0 * 60, "p50": _q(hist, 0.5), "p90": _q(hist, 0.9), "p99": _q(hist, 0.99), "n": sum(hist)})
        return out

    def host_series(self, window, metric: str, env=None, host=None, step: int = 1, top: int = 6) -> dict:
        """The `top` hosts in scope by `metric` over the range and their value per bar. lines / errors: counts; error_rate: 0..1, ranked by
        errors; p95: ms, ranked by the host's p95 over the range; sessions: distinct sessions. Hosts with nothing to show are left out."""
        scope = self._scope(env, host)
        mins = self._mins(window)
        tot: dict[int, list] = {}                   # host -> [rank value, shown total]
        val: dict[tuple, float] = {}                # (host, bar) -> value
        ts: dict[int, dict] = {}                    # sessions: host -> session -> active minutes of the range (one int: few allocations under the lock)
        with self.lock:
            names = list(self.hosts)
            if metric == "p95":
                ht: dict[int, list] = {}
                hb: dict[tuple, list] = {}
                for m in mins:
                    for (hn, _s), hh in self.lat.get(m, {}).items():
                        h = self.hid.get(hn)
                        if scope is None or h in scope:
                            for acc in (ht.setdefault(h, [0] * (len(BOUNDS) + 1)), hb.setdefault((h, (m - mins[0]) // step), [0] * (len(BOUNDS) + 1))):
                                for i, c in enumerate(hh):
                                    acc[i] += c
                tot = {h: [_q(x, 0.95)] * 2 for h, x in ht.items() if sum(x)}
                val = {k: _q(x, 0.95) for k, x in hb.items()}
            elif metric == "sessions":
                for b, h, rec, mk in self._ses_scan(mins, scope):
                    sh = b * 5 - mins[0]
                    d = ts.get(h)
                    if d is None:
                        d = ts[h] = {}
                    sid = rec[6][0]
                    d[sid] = d.get(sid, 0) | (mk << sh if sh >= 0 else mk >> -sh)
            else:
                ln: dict[tuple, list] = {}
                for m in mins:
                    for (h, sev), n in self.cnt.get(m, {}).items():
                        if scope is None or h in scope:
                            x = ln.setdefault((h, (m - mins[0]) // step), [0, 0])
                            t = tot.setdefault(h, [0, 0])
                            x[0] += n; t[0] += n
                            if SEV_RANK[sev] >= 3:
                                x[1] += n; t[1] += n
                tot, val = _count_series(tot, ln, metric)
        if metric == "sessions":                    # distinct sessions per (host, bar), after the lock
            for h, d in ts.items():
                for m in d.values():
                    last = -1
                    while m:
                        lb = m & -m
                        i = (lb.bit_length() - 1) // step
                        if i != last:
                            val[(h, i)] = val.get((h, i), 0) + 1; last = i
                        m ^= lb
            tot = {h: [len(d)] * 2 for h, d in ts.items()}
        return host_series_out(tot, val, names, -(-len(mins) // step), mins[0], step, metric, top)

    def stats_internal(self) -> dict:
        a = sorted(self.add_ms)
        return {"hosts": len(self.hosts), "groups": len(self.ginfo), "minutes": len(self.cnt), "added": self.added,
                "add_ms_p95": round(a[int(len(a) * 0.95) - 1], 2) if len(a) >= 20 else None}


def session_link(info: tuple, traced: int) -> str:
    """Where a session opens: a user-sensor session in its own timeline; an app / SSO session that became spans in the trace search,
    by its pseudonym (the trace search resolves it, so the raw id never travels in a URL); anything else nowhere."""
    if info[1] == "rum" and info[4]:
        return f"/rum/sessions/{info[4]}"
    if info[1] == "sso" or (info[1] == "app" and (traced or info[5])):
        return f"/traces?session={info[0]}"
    return ""


def _count_series(tot: dict, ln: dict, metric: str) -> tuple[dict, dict]:
    """[lines, errors] per host and per (host, bar) -> (host -> [rank value, shown total], (host, bar) -> value) for lines / errors / error_rate."""
    if metric == "lines":
        return {h: [t[0]] * 2 for h, t in tot.items()}, {k: x[0] for k, x in ln.items()}
    if metric == "errors":
        return {h: [t[1]] * 2 for h, t in tot.items()}, {k: x[1] for k, x in ln.items()}
    return ({h: [t[1], round(t[1] / t[0], 5) if t[0] else None] for h, t in tot.items()},
            {k: (round(x[1] / x[0], 5) if x[0] else None) for k, x in ln.items()})


def host_series_out(tot: dict, val: dict, name, nb: int, m0: int, step: int, metric: str, top: int) -> dict:
    """Top hosts by rank value (zero / missing left out) and one row per bar: {"t", "<host>": value}. Counts are 0 in an empty bar,
    ratios and percentiles None. `name` maps a host key to its name (a list indexed by host id, or a dict)."""
    ranked = sorted(((h, v) for h, v in tot.items() if v[0]), key=lambda x: (-x[1][0], name[x[0]]))[:max(1, top)]
    empty = 0 if metric in ("lines", "errors", "sessions") else None
    series = []
    for i in range(nb):
        row = {"t": (m0 + i * step) * 60}
        for h, _v in ranked:
            row[name[h]] = val.get((h, i), empty)
        series.append(row)
    return {"hosts": [{"host": name[h], "total": v[1]} for h, v in ranked], "series": series}


def pools_of(rows: list[dict], minutes: int) -> list[dict]:
    """Host rows (host, pool, env, events, errors, warnings, critical, silent) grouped into pools."""
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
        out.append({**p, "envs": sorted(p["envs"]), "hosts": hs, "host_count": len(hs), "per_min": round(p["events"] / minutes, 1),
                    "error_rate": round(p["errors"] / p["events"], 4) if p["events"] else 0, "worst": hs[0]["host"] if hs and hs[0]["errors"] else None,
                    # a host far off its pool's volume (5x more or 5x less than the pool mean) is worth a look
                    "outliers": [r["host"] for r in hs if mean and len(ev) >= 3 and not r["silent"] and (r["events"] > 5 * mean or r["events"] < mean / 5)]})
    return sorted(out, key=lambda p: (-p["errors"], -p["events"]))
