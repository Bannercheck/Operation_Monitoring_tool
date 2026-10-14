"""Traces without instrumentation: spans rebuilt from what systems already log, and RED / Apdex service metrics on top.

A span is anything that took time on behalf of a request: an access-log line with a duration (nginx, Apache, IIS, Envoy, ALB),
an application log with a trace / request / correlation id and a duration, a browser or mobile step from the user sensors, an
OTLP span. Spans that share an id form a trace. Every span records how it joined its trace (`link`) and how sure that is:

    explicit  a shared W3C traceparent, B3, request / correlation id                      confidence 1.0
    session   the same user session (browser / mobile / SSO), no request id                confidence 0.7
    none      a span on its own (kept for the service metrics, stored when slow or failed)  -

So the waterfall can say what is known and what is inferred, instead of only showing what an agent instrumented.

Storage keeps every request in per-minute rollups (count, errors, latency histogram: RED, percentiles and Apdex for 100% of
traffic) and full spans only for a deterministic share of traces (hash of the trace id, so a trace is kept whole) plus every
slow or failed span. Settings: trace_sample_pct, trace_slow_ms, trace_apdex_ms, trace_retention_h, trace_rollup_days.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .models import SEV_RANK, Observation
from .privacy import path_template, pseudonym, scrub

UTC = timezone.utc
BOUNDS = [1, 2, 5, 10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000, 30000, 60000, 120000]
NB = len(BOUNDS) + 1                                     # last bucket: above 120 s

ID_KEYS = {
    "traceparent": "traceparent", "trace_id": "trace", "traceid": "trace", "trace.id": "trace", "otel.trace_id": "trace", "x-b3-traceid": "trace",
    "dd.trace_id": "trace", "trace": "trace",
    "span_id": "span", "spanid": "span", "span.id": "span", "otel.span_id": "span", "x-b3-spanid": "span",
    "parent_span_id": "parent", "parentspanid": "parent", "parent_id": "parent", "parent.id": "parent", "x-b3-parentspanid": "parent",
    "request_id": "request", "requestid": "request", "x-request-id": "request", "req_id": "request", "reqid": "request", "request.id": "request",
    "http.request_id": "request", "correlation_id": "request", "correlationid": "request", "x-correlation-id": "request", "correlation": "request",
    "activity_id": "request", "activityid": "request", "transaction_id": "request", "txn_id": "request", "msgid": "request",
    "session_id": "session", "sessionid": "session", "jsessionid": "session", "session.id": "session", "sid": "session",
    "user": "user", "user_id": "user", "userid": "user", "username": "user", "user.id": "user", "uid": "user", "cs-username": "user",
    "enduser.id": "user", "sap.user": "user", "user.name": "user",
}
DUR_KEYS = [("duration_ms", 1.0), ("elapsed_ms", 1.0), ("latency_ms", 1.0), ("response_time_ms", 1.0), ("took_ms", 1.0), ("time_ms", 1.0),
            ("rt_ms", 1.0), ("time-taken", 1.0), ("time_taken", 1.0), ("duration_us", 0.001), ("duration_micros", 0.001), ("duration_ns", 1e-6),
            ("duration_s", 1000.0), ("latency_s", 1000.0), ("request_time", 1000.0), ("elapsed_s", 1000.0), ("rt", 1000.0),
            ("duration", None), ("elapsed", None), ("latency", None), ("took", None), ("response_time", None)]
OP_KEYS = ("http.route", "route", "endpoint", "operation", "span.name", "operation_name", "url.path", "http.target", "path", "uri", "url", "http.url")
STATUS_KEYS = ("http.status_code", "status_code", "sc-status", "status", "http.status", "response_code", "statuscode")
METHOD_KEYS = ("http.method", "method", "cs-method", "http.request.method")
REQ_LINE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)")
MSG_MS = re.compile(r"\b(\d+(?:\.\d+)?)\s?ms\b")
TRACEPARENT = re.compile(r"\b00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}\b")
MSG_IDS = re.compile(r"\b(trace_?id|request_?id|req_?id|correlation_?id|x-request-id)[=:]\s*\"?([A-Za-z0-9\-_.]{6,64})", re.I)
DB_HINT = re.compile(r"\b(select|insert|update|delete)\b.+\b(from|into|set)\b", re.I)


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dur_value(v) -> float | None:
    """A duration without a unit in the key: "120ms", "1.2s", "350us"; a bare fraction under a minute is seconds, else ms."""
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|us|µs|ns)?\s*", v)
        if not m:
            return None
        x, unit = float(m[1]), (m[2] or "")
        if unit:
            return x * {"ms": 1, "s": 1000, "sec": 1000, "us": 0.001, "µs": 0.001, "ns": 1e-6}[unit]
        v = x
    x = _f(v)
    if x is None:
        return None
    return x * 1000 if (x != int(x) and x < 60) else x


def bucket(ms: float) -> int:
    return bisect_left(BOUNDS, ms)


def quantile(hist: list[int], q: float) -> float | None:
    n = sum(hist)
    if not n:
        return None
    want, acc = q * n, 0
    for i, c in enumerate(hist):
        if acc + c >= want and c:
            lo = BOUNDS[i - 1] if i > 0 else 0
            hi = BOUNDS[i] if i < len(BOUNDS) else BOUNDS[-1] * 2
            return round(lo + (hi - lo) * ((want - acc) / c), 1)
        acc += c
    return float(BOUNDS[-1])


def apdex(hist: list[int], t_ms: float) -> float | None:
    """Satisfied <= T, tolerating <= 4T (counted half); interpolated inside the histogram buckets."""
    n = sum(hist)
    if not n:
        return None

    def below(x: float) -> float:
        acc = 0.0
        for i, c in enumerate(hist):
            lo = BOUNDS[i - 1] if i > 0 else 0
            hi = BOUNDS[i] if i < len(BOUNDS) else BOUNDS[-1] * 2
            if x >= hi:
                acc += c
            elif x > lo:
                acc += c * (x - lo) / (hi - lo)
        return acc
    sat = below(t_ms); tol = below(4 * t_ms) - sat
    return round((sat + tol / 2) / n, 3)


def _attr(a: dict, keys) -> object:
    low = {k.lower(): v for k, v in a.items()}
    for k in keys:
        if k in low and low[k] not in (None, "", "-"):
            return low[k]
    return None


def span_of(o: Observation) -> dict | None:
    """The span an observation describes, or None when it carries neither a duration nor a trace / request id."""
    a = o.attributes or {}
    low = {str(k).lower(): v for k, v in a.items()}
    ids: dict[str, str] = {}
    for k, v in low.items():
        role = ID_KEYS.get(k)
        if role and v not in (None, "", "-") and role not in ids:
            ids[role] = str(v)[:128]
    text = o.raw or o.message or ""
    if "traceparent" not in ids and (m := TRACEPARENT.search(text)):
        ids["traceparent"] = m.group(0)
    if "trace" not in ids and "request" not in ids and (m := MSG_IDS.search(o.message or "")):
        ids["trace" if m[1].lower().startswith("trace") else "request"] = m[2]
    dur = None
    for k, mult in DUR_KEYS:
        if k in low and low[k] not in (None, "", "-"):
            dur = (_f(low[k]) * mult) if mult is not None and _f(low[k]) is not None else _dur_value(low[k])
            if dur is not None:
                break
    method = _attr(a, METHOD_KEYS)
    target = _attr(a, OP_KEYS)
    if target is None and isinstance(low.get("cs-uri-stem"), str):
        target = low["cs-uri-stem"]
    req = low.get("request")
    if (target is None or method is None) and isinstance(req, str) and (m := REQ_LINE.match(req)):
        method, target = method or m[1], target or m[2]
    if (target is None or method is None) and (m := REQ_LINE.match(o.message or "")):
        method, target = method or m[1], target or m[2]
    if dur is None and (ids or method) and (m := MSG_MS.search(o.message or "")):
        dur = float(m[1])
    trace = span = parent = None
    if tp := ids.get("traceparent"):
        parts = tp.split("-")
        if len(parts) >= 3:
            trace, parent = parts[1], parts[2]
    trace = trace or ids.get("trace") or ids.get("request")
    span = ids.get("span")
    parent = ids.get("parent") or parent
    if dur is None and not trace:
        return None
    if dur is not None and (dur < 0 or dur > 24 * 3600 * 1000):
        dur = None
    status = _attr(a, STATUS_KEYS)
    try:
        status = int(str(status)[:3]) if status is not None else 0
    except ValueError:
        status = 0
    sev = SEV_RANK.get(o.severity, 1)
    error = int(status >= 500 or (status == 0 and sev >= 3))
    kind = str(_attr(a, ("span.kind", "kind", "span_kind")) or "").lower()
    if kind not in ("server", "client", "internal", "producer", "consumer", "db", "external", "user", "mobile", "log"):
        if "db.system" in low or "db.statement" in low or DB_HINT.search(o.message or ""):
            kind = "db"
        elif "target" in low or "upstream" in low or "peer.service" in low:
            kind = "client"
        elif method or "request" in low:
            kind = "server"
        elif dur is None:
            kind = "log"
        else:
            kind = "internal"
    if target:
        op = f"{method + ' ' if method else ''}{path_template(str(target))}"
    else:
        op = str(_attr(a, ("span.name", "name", "operation")) or "")[:80] or (o.template or scrub(o.message or ""))[:80]
    svc = o.service if o.service and o.service not in ("http", "w3c", "access", "-") else \
        str(_attr(a, ("s-sitename", "cs-host", "host.name", "server_name")) or o.host or "http")
    start = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)
    if dur and not a.get("ts_is_start") and kind in ("server", "client") and o.parser in ("access", "w3c"):
        start = start - timedelta(milliseconds=dur)          # nginx / IIS stamp the line when the response is sent
    return {"trace_id": trace, "span_id": span, "parent_id": parent, "start": start, "duration_ms": round(dur, 3) if dur is not None else 0.0,
            "has_duration": dur is not None, "service": svc[:120], "operation": op[:160] or "-", "kind": kind, "status": status, "error": error,
            "env": o.environment or "", "host": (o.host or "")[:120], "user_ref": pseudonym(ids.get("user")), "session_id": (ids.get("session") or "")[:128],
            "link": "explicit" if trace else ("session" if ids.get("session") else "none"), "confidence": 1.0 if trace else (0.7 if ids.get("session") else 0.0),
            "ref": o.ref, "message": scrub(o.message or "")[:300], "severity": o.severity,
            "attrs": {k: v for k, v in (("http.method", method), ("http.status", status or None), ("upstream_ms", low.get("upstream_duration_ms") or
                     (round(_f(low["upstream_duration_s"]) * 1000, 1) if _f(low.get("upstream_duration_s")) is not None else None))) if v}}


def _minute(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


class TraceStore:
    """Spans + per-minute service rollups in the product database (SQLite or PostgreSQL)."""
    COLS = ("trace_id", "span_id", "parent_id", "ts", "start_ms", "duration_ms", "service", "operation", "kind", "status", "error", "env",
            "host", "user_ref", "session_id", "link", "confidence", "ref", "message", "severity", "attrs")

    def __init__(self, db, settings=None):
        self.db = db
        self.settings = settings or (lambda: {})
        self.lock = threading.Lock()
        self._agg: dict[tuple, list] = {}                    # (minute, service, operation, kind, env) -> [n, err, sum, max, hist]
        from collections import OrderedDict
        self._held: OrderedDict = OrderedDict()              # trace id -> unsampled spans, kept ~2 min in case a user step claims the trace
        self._held_n = 0
        self._force: OrderedDict = OrderedDict()             # trace ids started by a user (browser / mobile): every span of them is kept
        self._last_prune = 0.0
        self.received = 0
        self.kept = 0
        pk, dbl = db.pk, "DOUBLE PRECISION"
        db._exec(f"""CREATE TABLE IF NOT EXISTS spans (id {pk}, trace_id TEXT, span_id TEXT, parent_id TEXT, ts TEXT, start_ms {dbl},
            duration_ms {dbl}, service TEXT, operation TEXT, kind TEXT, status INTEGER, error INTEGER, env TEXT, host TEXT, user_ref TEXT,
            session_id TEXT, link TEXT, confidence {dbl}, ref TEXT, message TEXT, severity TEXT, attrs TEXT)""")
        for ix in ("trace_id", "ts", "service, ts", "session_id"):
            db._exec(f"CREATE INDEX IF NOT EXISTS spans_{ix.replace(', ', '_')} ON spans({ix})")
        db._exec(f"""CREATE TABLE IF NOT EXISTS svc_minute (minute TEXT, service TEXT, operation TEXT, kind TEXT, env TEXT, n INTEGER,
            err INTEGER, sum_ms {dbl}, max_ms {dbl}, hist TEXT, PRIMARY KEY (minute, service, operation, kind, env))""")
        db._exec("CREATE INDEX IF NOT EXISTS svc_minute_service ON svc_minute(service, minute)")

    def _cfg(self, k: str, d):
        now = time.time()
        if now - getattr(self, "_cfg_at", 0) > 10:              # settings come from a file: read at most every 10 s, not per span
            try:
                self._cfg_cache = dict(self.settings() or {})
            except Exception:  # noqa: BLE001
                self._cfg_cache = {}
            self._cfg_at = now
        try:
            return type(d)(self._cfg_cache.get(k, d) or d)
        except (TypeError, ValueError):
            return d

    # ---------------------------------------------------------------- write
    def sampled(self, trace_id: str | None) -> bool:
        pct = self._cfg("trace_sample_pct", 10)
        if not trace_id or pct <= 0:
            return False
        return pct >= 100 or int(hashlib.sha1(trace_id.encode()).hexdigest()[:8], 16) % 10000 < pct * 100

    def add_spans(self, spans: list[dict], keep_all: bool = False) -> int:
        """Rollups for every span with a duration; full rows for sampled traces, slow or failed spans and user / mobile steps."""
        slow = self._cfg("trace_slow_ms", 1000)
        rows = []
        spans = list(spans)
        with self.lock:
            cut = time.time() - 120
            while self._held and (self._held_n > 50000 or next(iter(self._held.values()))[0]["_held_at"] < cut):
                _, old = self._held.popitem(last=False); self._held_n -= len(old)
            i = 0
            while i < len(spans):                            # index loop: a claimed trace appends its held spans to this batch
                sp = spans[i]; i += 1
                if sp.get("_counted"):
                    counted = True
                else:
                    counted = False; sp["_counted"] = True; self.received += 1
                if not counted and sp.get("has_duration", True) and sp["kind"] not in ("log",):
                    key = (_minute(sp["start"]), sp["service"], sp["operation"], sp["kind"], sp["env"])
                    a = self._agg.get(key)
                    if a is None:
                        a = self._agg[key] = [0, 0, 0.0, 0.0, [0] * NB]
                    d = sp["duration_ms"]
                    a[0] += 1; a[1] += sp["error"]; a[2] += d; a[3] = max(a[3], d); a[4][bucket(d)] += 1
                tid = sp.get("trace_id")
                from_user = str(sp.get("ref", "")).startswith("rum:")
                if from_user and tid:                            # tail sampling for user journeys: claim the trace, release held spans
                    if tid not in self._force:
                        self._force[tid] = time.time()
                        if len(self._force) > 200000:
                            self._force.popitem(last=False)
                        claimed = self._held.pop(tid, None)
                        if claimed:
                            self._held_n -= len(claimed)
                            spans = spans + claimed
                keep = (keep_all or from_user or sp["error"] or sp["duration_ms"] >= slow or sp["kind"] in ("user", "mobile")
                        or (tid is not None and tid in self._force) or self.sampled(tid))
                if not keep:
                    if tid and sp["link"] == "explicit":
                        sp["_held_at"] = time.time()
                        self._held.setdefault(tid, []).append(sp); self._held_n += 1
                    continue
                if not sp.get("trace_id"):
                    sp["trace_id"] = "s-" + hashlib.sha1(f"{sp['ref']}|{sp['start'].isoformat()}|{sp['service']}".encode()).hexdigest()[:24]
                st = sp["start"]
                rows.append((sp["trace_id"], sp.get("span_id"), sp.get("parent_id"), st.isoformat(), st.timestamp() * 1000, sp["duration_ms"],
                             sp["service"], sp["operation"], sp["kind"], sp["status"], sp["error"], sp["env"], sp["host"], sp["user_ref"],
                             sp["session_id"], sp["link"], sp["confidence"], sp["ref"], sp["message"], sp["severity"],
                             json.dumps(sp.get("attrs") or {}, ensure_ascii=False, default=str)[:2000]))
        if rows:
            self.db.executemany(f"INSERT INTO spans ({', '.join(self.COLS)}) VALUES ({', '.join('?' * len(self.COLS))})", rows)
            self.kept += len(rows)
        self._flush(closed_only=True)
        if time.time() - self._last_prune > 3600:
            self.prune()
        return len(rows)

    def add_events(self, events: list[Observation], keep_all: bool = False) -> int:
        spans = [sp for sp in (span_of(o) for o in events) if sp]
        return self.add_spans(spans, keep_all=keep_all) if spans else 0

    def _flush(self, closed_only: bool = True) -> None:
        now_min = _minute(datetime.now(UTC))
        with self.lock:
            out = {k: v for k, v in self._agg.items() if not closed_only or k[0] < now_min}
            for k in out:
                del self._agg[k]
        if not out:
            return
        keys = list(out)
        have: dict[tuple, dict] = {}
        for i in range(0, len(keys), 200):                      # merge with what an earlier flush of the same minute wrote
            chunk = keys[i:i + 200]
            cond = " OR ".join("(minute=? AND service=? AND operation=? AND kind=? AND env=?)" for _ in chunk)
            for r in self.db._exec(f"SELECT * FROM svc_minute WHERE {cond}", tuple(x for k in chunk for x in k)):
                have[(r["minute"], r["service"], r["operation"], r["kind"], r["env"])] = r
        ins, upd = [], []
        for k, (n, err, sm, mx, hist) in out.items():
            if k in have:
                r = have[k]; h0 = json.loads(r["hist"] or "[]") + [0] * NB
                upd.append((r["n"] + n, r["err"] + err, r["sum_ms"] + sm, max(r["max_ms"], mx), json.dumps([a + b for a, b in zip(h0[:NB], hist)]), *k))
            else:
                ins.append((*k, n, err, sm, mx, json.dumps(hist)))
        self.db.executemany("INSERT INTO svc_minute (minute, service, operation, kind, env, n, err, sum_ms, max_ms, hist) VALUES (?,?,?,?,?,?,?,?,?,?)", ins)
        self.db.executemany("UPDATE svc_minute SET n=?, err=?, sum_ms=?, max_ms=?, hist=? WHERE minute=? AND service=? AND operation=? AND kind=? AND env=?", upd)

    def flush(self) -> None:
        self._flush(closed_only=False)

    def prune(self) -> dict:
        self._last_prune = time.time()
        now = datetime.now(UTC)
        s = (now - timedelta(hours=self._cfg("trace_retention_h", 72))).isoformat()
        m = _minute(now - timedelta(days=self._cfg("trace_rollup_days", 30)))
        a = self.db._exec("SELECT COUNT(*) AS n FROM spans WHERE ts < ?", (s,))[0]["n"]
        self.db._exec("DELETE FROM spans WHERE ts < ?", (s,))
        self.db._exec("DELETE FROM svc_minute WHERE minute < ?", (m,))
        return {"spans_removed": int(a)}

    # ---------------------------------------------------------------- read: rollups
    def _rollups(self, since: str | None, service: str | None = None, env: str | None = None) -> list[dict]:
        where, p = [], []
        if since:
            where.append("minute >= ?"); p.append(since)
        if service:
            where.append("service = ?"); p.append(service)
        if env:
            where.append("env = ?"); p.append(env)
        rows = self.db._exec("SELECT * FROM svc_minute" + (" WHERE " + " AND ".join(where) if where else ""), tuple(p))
        for r in rows:
            r["hist"] = (json.loads(r["hist"] or "[]") + [0] * NB)[:NB]
        with self.lock:                                       # the minute still being filled
            for (mi, sv, op, kd, ev), (n, err, sm, mx, hist) in self._agg.items():
                if (not since or mi >= since) and (not service or sv == service) and (not env or ev == env):
                    rows.append({"minute": mi, "service": sv, "operation": op, "kind": kd, "env": ev, "n": n, "err": err, "sum_ms": sm, "max_ms": mx, "hist": list(hist)})
        return rows

    @staticmethod
    def _since(minutes: int | None) -> str | None:
        return _minute(datetime.now(UTC) - timedelta(minutes=minutes)) if minutes else None

    @staticmethod
    def _stats(rows: list[dict], span_min: float, t_ms: float) -> dict:
        n = sum(r["n"] for r in rows); err = sum(r["err"] for r in rows)
        hist = [sum(h) for h in zip(*[r["hist"] for r in rows])] if rows else [0] * NB
        return {"calls": n, "errors": err, "error_rate": round(err / n, 4) if n else 0.0, "rps": round(n / max(span_min, 1) / 60, 3),
                "avg_ms": round(sum(r["sum_ms"] for r in rows) / n, 1) if n else None, "max_ms": max((r["max_ms"] for r in rows), default=None),
                "p50_ms": quantile(hist, 0.5), "p95_ms": quantile(hist, 0.95), "p99_ms": quantile(hist, 0.99), "apdex": apdex(hist, t_ms)}

    def _span_minutes(self, rows: list[dict], minutes: int | None) -> float:
        if minutes:
            return float(minutes)
        ms = sorted({r["minute"] for r in rows})
        if not ms:
            return 1.0
        a, b = (datetime.strptime(x, "%Y-%m-%dT%H:%M") for x in (ms[0], ms[-1]))
        return (b - a).total_seconds() / 60 + 1

    INBOUND = ("server", "internal", "consumer", "user", "mobile")

    def services(self, minutes: int | None = 60, env: str | None = None) -> list[dict]:
        rows = [r for r in self._rollups(self._since(minutes), env=env) if r["kind"] in self.INBOUND]
        span = self._span_minutes(rows, minutes); t = self._cfg("trace_apdex_ms", 500)
        by: dict[str, list] = defaultdict(list)
        for r in rows:
            by[r["service"]].append(r)
        out = []
        for svc, rs in by.items():
            st = self._stats(rs, span, t)
            per = defaultdict(lambda: [0, 0, [0] * NB])
            for r in rs:
                x = per[r["minute"]]; x[0] += r["n"]; x[1] += r["err"]; x[2] = [a + b for a, b in zip(x[2], r["hist"])]
            series = [{"t": m, "n": v[0], "err": v[1], "p95": quantile(v[2], 0.95)} for m, v in sorted(per.items())]
            out.append({"service": svc, **st, "operations": len({r["operation"] for r in rs}), "envs": sorted({r["env"] for r in rs if r["env"]}), "series": series[-120:]})
        return sorted(out, key=lambda x: (-x["error_rate"], -x["calls"]))

    def service(self, service: str, minutes: int | None = 60, env: str | None = None) -> dict:
        rows = self._rollups(self._since(minutes), service=service, env=env)
        span = self._span_minutes(rows, minutes); t = self._cfg("trace_apdex_ms", 500)
        inbound = [r for r in rows if r["kind"] in self.INBOUND]
        per = defaultdict(lambda: [0, 0, [0] * NB])
        for r in inbound:
            x = per[r["minute"]]; x[0] += r["n"]; x[1] += r["err"]; x[2] = [a + b for a, b in zip(x[2], r["hist"])]
        series = [{"t": m, "n": v[0], "rps": round(v[0] / 60, 3), "err": v[1], "error_rate": round(v[1] / v[0], 4) if v[0] else 0,
                   "p50": quantile(v[2], 0.5), "p95": quantile(v[2], 0.95), "p99": quantile(v[2], 0.99)} for m, v in sorted(per.items())]

        def table(kinds) -> list[dict]:
            by = defaultdict(list)
            for r in rows:
                if r["kind"] in kinds:
                    by[r["operation"]].append(r)
            return sorted(({"operation": op, **self._stats(rs, span, t)} for op, rs in by.items()), key=lambda x: -x["calls"])[:50]
        return {"service": service, "summary": self._stats(inbound, span, t), "series": series, "operations": table(self.INBOUND),
                "db_calls": table(("db",)), "external_calls": table(("client", "external", "producer")),
                "slow_traces": self.search(service=service, minutes=minutes, env=env, order="duration", limit=15)}

    # ---------------------------------------------------------------- read: traces
    def search(self, service: str | None = None, operation: str | None = None, minutes: int | None = 60, env: str | None = None,
               min_ms: float | None = None, errors: bool = False, q: str | None = None, session: str | None = None, user: str | None = None,
               order: str = "recent", limit: int = 50) -> list[dict]:
        where, p = [], []
        if minutes:
            where.append("ts >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        for col, v in (("service", service), ("operation", operation), ("env", env), ("session_id", session), ("user_ref", user)):
            if v:
                where.append(f"{col} = ?"); p.append(v)
        if q:
            where.append("(trace_id = ? OR operation LIKE ? OR message LIKE ?)"); p += [q, f"%{q}%", f"%{q}%"]
        if errors:
            where.append("error = 1")
        cond = " WHERE " + " AND ".join(where) if where else ""
        having = " HAVING MAX(start_ms + duration_ms) - MIN(start_ms) >= ?" if min_ms else ""
        if min_ms:
            p.append(float(min_ms))
        orderby = "dur DESC" if order == "duration" else "s DESC"
        lim = max(1, min(int(limit), 500))
        hits = self.db._exec(f"SELECT trace_id, MIN(start_ms) AS s, MAX(start_ms + duration_ms) AS e, MAX(start_ms + duration_ms) - MIN(start_ms) AS dur "
                             f"FROM spans{cond} GROUP BY trace_id{having} ORDER BY {orderby} LIMIT {lim}", tuple(p))
        if not hits:
            return []
        ids = [h["trace_id"] for h in hits]
        rows = self.db._exec(f"SELECT trace_id, service, operation, kind, start_ms, duration_ms, error, link, user_ref, session_id, env FROM spans "
                             f"WHERE trace_id IN ({','.join('?' * len(ids))})", tuple(ids))
        by = defaultdict(list)
        for r in rows:
            by[r["trace_id"]].append(r)
        out = []
        for h in hits:
            sp = sorted(by[h["trace_id"]], key=lambda r: (r["start_ms"], -r["duration_ms"]))
            root = next((r for r in sp if r["kind"] in ("user", "mobile")), None) or next((r for r in sp if r["kind"] == "server"), None) or sp[0]
            out.append({"trace_id": h["trace_id"], "start": datetime.fromtimestamp(h["s"] / 1000, UTC).isoformat(), "duration_ms": round(h["dur"], 1),
                        "spans": len(sp), "services": sorted({r["service"] for r in sp}), "root_service": root["service"], "root_operation": root["operation"],
                        "error": int(any(r["error"] for r in sp)), "links": sorted({r["link"] for r in sp}), "user_ref": next((r["user_ref"] for r in sp if r["user_ref"]), ""),
                        "session_id": next((r["session_id"] for r in sp if r["session_id"]), ""), "env": next((r["env"] for r in sp if r["env"]), "")})
        return out

    def trace(self, trace_id: str) -> dict | None:
        rows = self.db._exec("SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ms, duration_ms DESC", (trace_id,))
        if not rows:
            return None
        return waterfall(rows)


def waterfall(rows: list[dict]) -> dict:
    """Parent of each span: its explicit parent id when the logs carried one, otherwise the tightest earlier span of another
    service whose time range contains it (marked `parent_inferred`); depth and offset for the drawing."""
    for r in rows:
        r["attrs"] = json.loads(r["attrs"]) if isinstance(r.get("attrs"), str) and r["attrs"] else (r.get("attrs") or {})
        r["end_ms"] = r["start_ms"] + (r["duration_ms"] or 0)
    t0 = min(r["start_ms"] for r in rows); t1 = max(r["end_ms"] for r in rows)
    by_span = {r["span_id"]: r for r in rows if r.get("span_id")}
    for i, r in enumerate(rows):                              # keys first: an explicit parent may sort after its child
        r["key"] = r.get("span_id") or f"#{i}"
    for i, r in enumerate(rows):
        par = by_span.get(r.get("parent_id")) if r.get("parent_id") else None
        if par is r:
            par = None
        r["parent_inferred"] = False
        if par is None:
            best = None
            for c in rows[:i]:
                if c is r or c["kind"] == "log":
                    continue
                outgoing = r["kind"] in ("db", "client", "external", "producer") and c["kind"] not in ("db", "client", "external", "producer")
                if c["start_ms"] <= r["start_ms"] + 0.5 and c["end_ms"] + 0.5 >= r["end_ms"] and (c["service"] != r["service"] or outgoing or c["kind"] in ("user", "mobile")):
                    if best is None or (c["end_ms"] - c["start_ms"]) <= (best["end_ms"] - best["start_ms"]):
                        best = c
            par = best
            r["parent_inferred"] = par is not None
        r["parent_key"] = par["key"] if par else None
    keyed = {r["key"]: r for r in rows}

    def depth(r, seen=()) -> int:
        p = keyed.get(r["parent_key"]) if r["parent_key"] else None
        return 0 if p is None or r["key"] in seen else 1 + depth(p, seen + (r["key"],))
    spans = []
    for r in rows:
        own = r["duration_ms"] - sum(c["duration_ms"] for c in rows if c.get("parent_key") == r["key"] and c["kind"] != "log")
        spans.append({**{k: r[k] for k in ("key", "parent_key", "parent_inferred", "service", "operation", "kind", "status", "error", "env", "host",
                                          "link", "confidence", "ref", "message", "severity", "attrs", "user_ref", "session_id", "span_id")},
                      "start": datetime.fromtimestamp(r["start_ms"] / 1000, UTC).isoformat(), "offset_ms": round(r["start_ms"] - t0, 2),
                      "duration_ms": round(r["duration_ms"] or 0, 2), "self_ms": round(max(own, 0), 2), "depth": depth(r)})
    svc_time = defaultdict(float)
    for s in spans:
        if s["kind"] != "log":
            svc_time[s["service"]] += s["self_ms"]
    return {"trace_id": rows[0]["trace_id"], "start": datetime.fromtimestamp(t0 / 1000, UTC).isoformat(), "duration_ms": round(t1 - t0, 2),
            "spans": spans, "services": sorted(svc_time.items(), key=lambda x: -x[1]), "errors": sum(s["error"] for s in spans),
            "links": dict(sorted(((k, sum(1 for s in spans if s["link"] == k)) for k in {s["link"] for s in spans}))),
            "inferred_parents": sum(1 for s in spans if s["parent_inferred"])}


def store_for_observations(obs: list[Observation], settings=None) -> TraceStore:
    """A throw-away in-memory store for an uploaded dataset: every span kept, every minute flushed."""
    from .db import Database
    st = TraceStore(Database(":memory:"), settings=lambda: {**(settings() if settings else {}), "trace_sample_pct": 100})
    st.add_events(list(obs), keep_all=True)
    st.flush()
    return st
