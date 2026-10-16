"""Log explorer backend (docs/design/logs.md section 3 and 5): search, histogram, facets, groups, live tail, line detail, context,
fields, saved searches. One query language (logquery), one line view over the ring and the stored rows (logstore).

Sources, cheapest first:
  counters  the per-minute facet cube in Rollup: exact counts for level / service / host / env / pool / file over the last hour
  stored    live_batches rows, read newest first by ts_max with a keyset (ts_max, id), pruned by severity mask / host / file / a
            text prefilter before decoding; lines leave a heap in (ts, seq) order once no unread row can hold a newer line
  ring      the in-memory ring (no database, and the live tail, which follows arrival order by seq)
  history   older than the line retention: only totals and ERROR+ per host / env / pool (slo_minute)

Every scan has a budget (1.5 s, 600,000 lines, 25,000 rows; at most two at once) and says how far it got (`scan.frontier`,
`complete`, `stopped_by`); counts are `exact`, `estimated` (scaled by the cube) or `at_least`. Team scope is applied first everywhere.
"""
from __future__ import annotations

import base64
import hashlib
import heapq
import itertools
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import logquery as lq
from . import logstore
from . import privacy
from .logstore import DictLine, ObsLine, ROW_COLS, ROW_SPAN_S, iso_s, row_bytes

UTC = timezone.utc
SEVS_DESC = ("CRITICAL", "ERROR", "WARN", "INFO", "DEBUG")
BUDGET_MS, BUDGET_LINES, BUDGET_ROWS = 1500, 600_000, 25_000
COUNT_MS = 600                       # first page: keep counting after the page is full, for an exact or better-estimated total
SAMPLE_LINES = 150_000               # a stored range expected to hold more lines is read as time slices and scaled (sample: k)
TAIL_MAX, TAIL_CAP_S, TAIL_MIN_GAP = 500, 250, 1.0
CONTEXT_S = 1800
MSG_SHOW = 2000
MSG_DETAIL = 16384
FACET_FIELDS = ("level", "service", "host", "env", "pool", "source")
CACHE_S = 5.0


class Busy(Exception):
    pass


class TooFast(Exception):
    def __init__(self, retry_ms: int):
        super().__init__("too_fast")
        self.retry_ms = retry_ms


class BadCursor(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _us(ts: str) -> int:
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return round(t.timestamp() * 1_000_000)


def _iso_ms(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds")


def _dt(v: str) -> datetime:
    from .timerange import _dt as tr_dt
    return tr_dt(v)


def step_seconds(sec: float) -> int:
    for s in (1, 2, 5, 10, 15, 30, 60):
        if sec / s <= 126:
            return s
    return 60


class Budget:
    def __init__(self, ms: Optional[int] = None, lines: Optional[int] = None, rows: Optional[int] = None, cancel: Optional[threading.Event] = None):
        self.t0 = time.perf_counter()
        self.end = self.t0 + (BUDGET_MS if ms is None else ms) / 1000.0
        self.max_lines = BUDGET_LINES if lines is None else lines
        self.max_rows = BUDGET_ROWS if rows is None else rows
        self.lines = self.rows = 0
        self.cold = 0                                   # rows read from the cold archive (logtiers): the answer says "cold"
        self.cancel = cancel

    def over(self) -> Optional[str]:
        if self.cancel is not None and self.cancel.is_set():
            return "cancelled"
        if time.perf_counter() > self.end:
            return "budget_time"
        if self.lines >= self.max_lines:
            return "budget_lines"
        if self.rows >= self.max_rows:
            return "budget_rows"
        return None

    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)


class Ctx:
    """What the compiled predicates need from the live counters: pool and environment of a host (memoised per request)."""
    def __init__(self, rollup):
        self.r = rollup
        self._pool: dict = {}

    def pool_of(self, host: str) -> str:
        p = self._pool.get(host)
        if p is None:
            p = self._pool[host] = self.r.pool_of(host) if host and host != "-" else "-"
        return p

    def env_of(self, host: str, fallback: str = "") -> str:
        """The host's environment as the counters know it (the cube's env), else the line's own: one meaning for counts,
        filters and the env shown on a line (a host's lines did not all carry the env, so line env and cube env disagreed)."""
        return self.r.host_env.get(host) or fallback or "unknown"


class Q:
    """A parsed request query: AST, normalized text, plan, predicate (with the team scope ANDed in). `apps(value, wild, scope)`
    resolves app:<name> clauses to the application's member hosts / services as this scope may see them (logquery.expand_apps);
    the normalized text keeps app:<name>, the plan and predicates run on the expansion."""
    def __init__(self, text: str, ctx: Ctx, scope, apps=None):
        self.text = text or ""
        p = lq.parse(self.text)
        self.warnings = list(p.warnings)
        self.norm = lq.normalize(p.node)
        self.node, aw, self.app_fp = lq.expand_apps(p.node, (lambda v, w: apps(v, w, scope)) if apps is not None else None)
        self.warnings += aw
        self.plan = lq.plan(self.node)
        self.ctx = ctx
        self.scope = scope
        pred = lq.compile(self.node, ctx)
        if scope is not None:
            allows = scope.allows
            self.pred = lambda v, f=pred: allows(v.host) and f(v)
        else:
            self.pred = pred
        if not self.plan.facet_only:
            self.warnings.append(lq.warning("scan_needed"))

    def scope_key(self) -> str:
        fp = f"+{self.app_fp}" if getattr(self, "app_fp", "") else ""
        if self.scope is None:
            return "*" + fp
        return hashlib.sha1("\n".join(sorted(self.scope.hosts)).encode()).hexdigest()[:10] + fp

    def key_pred(self, node=None):
        """Predicate over cube keys (facet-only), the scope included."""
        f = lq.compile_key(self.node if node is None else node, self.ctx)
        if self.scope is not None:
            allows = self.scope.allows
            return lambda sev, svc, src, host: allows(host) and f(sev, svc, src, host)
        return f


class LogSearch:
    def __init__(self, live, ranges=None, history=None, traces=None, kb=None, users=None, settings_fn=None):
        self.live, self.ranges, self.history, self.traces = live, ranges, history, traces
        self.kb = kb if kb is not None else live.db
        self.users = users
        self.app_resolver = None                    # (value, wild, scope) -> (hosts, services, globs) | None: Applications.query_members
        self.app_names = None                       # scope -> visible application names (autocomplete)
        self.sem = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self._cache: dict = {}
        self._polls: dict = {}                      # (user, tab) -> last tail poll epoch
        self._active: dict = {}                     # (user, tab) -> cancel event of the running scan
        self._v1 = (0.0, True)
        self._oldest = (0.0, "")
        self._rollup0 = live.rollup                 # the counters this process started with, and the newest stored line from before:
        self._pre_max = None                        # a range after it is fully counted even when the counters are younger than it
        if live.db is not None:
            try:
                if getattr(live.db, "hot", False):             # hot day files: an index probe per file
                    m = live.db.ts_edge(True, iso_s(datetime.now(UTC)))
                else:
                    r = live.db._exec("SELECT MAX(ts_max) AS m FROM live_batches WHERE ts_max <= ?", (iso_s(datetime.now(UTC)),))
                    m = r[0]["m"] if r else None
                self._pre_max = datetime.fromisoformat(m).timestamp() + 1 if m else None
            except Exception:  # noqa: BLE001
                self._pre_max = float("inf")
        if settings_fn is not None:
            try:
                logstore.configure(settings_fn())
            except Exception:  # noqa: BLE001
                pass
        if self.kb is not None:
            self.kb._exec(f"""CREATE TABLE IF NOT EXISTS log_searches (id {self.kb.pk}, owner TEXT NOT NULL, name TEXT NOT NULL, query TEXT NOT NULL,
                range TEXT DEFAULT '', columns TEXT DEFAULT '[]', group_similar INTEGER DEFAULT 0, shared INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
                UNIQUE (owner, name))""")

    # ---------------------------------------------------------------- common pieces
    def ctx(self) -> Ctx:
        return Ctx(self.live.rollup)

    def query(self, text: str, scope) -> Q:
        q = Q(text, self.ctx(), scope, self.app_resolver)
        self._attr_warnings(q, scope)
        return q

    def pin_range(self, last=None, start=None, end=None, round_s: int = 5) -> tuple:
        """(start, end, live). `last` minutes back from now: start aligned to the minute (the counters are exact), end at the next
        5-second boundary (every endpoint pins the same range, so counts and lines agree and answers can be shared for 5 s); or an
        absolute ISO start / end, down to one second. The answer echoes the pinned range: later pages send it back, never drift."""
        now = datetime.now(UTC)
        if start:
            s = _dt(start)
            e = _dt(end) if end else now
            if e < s:
                s, e = e, s
            if e - s < timedelta(seconds=1):
                e = s + timedelta(seconds=1)
            return s, e, e >= now - timedelta(seconds=90)
        last = max(1, min(int(last or 15), 576_000))
        e = datetime.fromtimestamp(math.ceil(now.timestamp() / round_s) * round_s, UTC)
        s = (now - timedelta(minutes=last)).replace(second=0, microsecond=0)
        return s, e, True

    def retention(self) -> dict:
        now = time.time()
        if now - self._oldest[0] > 60 and self.live.db is not None:
            try:
                if getattr(self.live.db, "hot", False):
                    o = self.live.db.ts_edge(False) or ""
                else:
                    r = self.live.db._exec("SELECT MIN(ts_max) AS o FROM live_batches")
                    o = (r[0]["o"] or "") if r else ""
                self._oldest = (now, o)
            except Exception:  # noqa: BLE001
                self._oldest = (now, "")
        out = {"hours": float(self.live.retention_h), "oldest": self._oldest[1] or None}
        cold = self.cold
        if cold is not None and self.live.db is not None:       # ranges back to the oldest archived day are searched (source "cold")
            co, lo, hz = cold.oldest_start(), self._hot_lo(), self._horizon()
            out["cold"] = {"oldest": co.isoformat() if co else None, "hot_from": lo.isoformat() if lo else None,
                           "horizon": hz.isoformat() if hz else None, "days": len(cold.days())}
        return out

    # ---------------------------------------------------------------- tiers (logtiers): hot day files, then the cold archive
    @property
    def cold(self):
        return getattr(self.live, "cold", None)

    def _hot_lo(self) -> Optional[datetime]:
        """Start of the oldest hot day file: older rows are only in the cold archive. None when there is no hot day file."""
        return self.live.db.oldest_start() if getattr(self.live.db, "hot", False) else None

    def _horizon(self) -> Optional[datetime]:
        """The oldest instant lines are kept for (oldest cold or hot day); None without a cold tier (the line retention rules)."""
        cold = self.cold
        if cold is None or self.live.db is None:
            return None
        cs = [x for x in (cold.oldest_start(), self._hot_lo()) if x is not None]
        return min(cs) if cs else datetime.now(UTC)

    def _past_retention(self, e: datetime) -> bool:
        return self.live.db is not None and e < datetime.now(UTC) - timedelta(hours=float(self.live.retention_h))

    def _expired(self, e: datetime) -> bool:
        """No stored line can be in a range ending at e: older than the cold archive or, without one, the line retention."""
        if self.live.db is None:
            return False
        hz = self._horizon()
        if hz is not None:
            return e <= hz
        return self._past_retention(e)

    def _history_ok(self, q: Q) -> bool:
        """The per-minute history answers this query's histogram (host / env / pool / level>=error only)."""
        if self.history is None:
            return False
        for leaf in lq._leaves(q.node):
            if isinstance(leaf, lq.Field) and not leaf.attr and leaf.name in ("host", "env", "pool"):
                continue
            if isinstance(leaf, lq.Field) and leaf.name == "level" and leaf.levels == frozenset({"ERROR", "CRITICAL"}):
                continue
            return False
        return True

    def _v1_present(self) -> bool:
        now = time.time()
        if now - self._v1[0] > 60:
            self._v1 = (now, (not self.live._v2) or logstore.has_v1(self.live.db))
        return self._v1[1]

    def _cached(self, key, fn):
        now = time.time()
        with self.lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        val = fn()
        with self.lock:
            if len(self._cache) > 256:
                for k in [k for k, v in self._cache.items() if v[0] <= now][:128] or list(self._cache)[:64]:
                    self._cache.pop(k, None)
            self._cache[key] = (now + CACHE_S, val)
        return val

    def _scan_slot(self, who=None):
        """Acquire one of the two scan slots (3 s wait, then Busy); a newer request of the same (user, tab) and endpoint cancels the
        older scan. Callers key it by endpoint: the page's search, histogram and facets run together and cancelled each other."""
        ev = threading.Event()
        if who is not None:
            with self.lock:
                old = self._active.get(who)
                if old is not None:
                    old.set()
                self._active[who] = ev
        if not self.sem.acquire(timeout=3):
            raise Busy()
        return ev

    def _release(self, who, ev) -> None:
        self.sem.release()
        if who is not None:
            with self.lock:
                if self._active.get(who) is ev:
                    self._active.pop(who, None)

    def cube_ok(self, s: datetime, e: datetime, live: bool) -> bool:
        """The facet cube answers [s, e) exactly: whole minutes (or up to now), inside the kept hour, counters running before s."""
        r = self.live.rollup
        st, et = s.timestamp(), e.timestamp()
        if st % 60 or (et % 60 and not live):
            return False
        m0, m1 = int(st // 60), int(math.ceil(et / 60) - 1)
        if m1 < m0 or not r.covers((m0, m1)):
            return False
        if r.started <= m0 * 60 + 1:
            return True
        return r is self._rollup0 and (self._pre_max is None or m0 * 60 >= self._pre_max)   # nothing stored in the range predates the counters

    def _mins(self, s: datetime, e: datetime) -> tuple:
        return int(s.timestamp() // 60), int(math.ceil(e.timestamp() / 60) - 1)

    def cube_count(self, q: Q, s: datetime, e: datetime, node=None) -> tuple:
        """(lines, minutes over the key cap) for a facet-only node over [s, e) from the cube."""
        m0, m1 = self._mins(s, e)
        r = self.live.rollup
        mins, over = r.cube(m0, m1)
        kp = q.key_pred(node)
        memo: dict = {}
        n = 0
        dec = r.cube_key
        for _m, d in mins:
            for k, c in d.items():
                ok = memo.get(k)
                if ok is None:
                    ok = memo[k] = kp(*dec(k))
                if ok:
                    n += c
        return n, over

    # ---------------------------------------------------------------- reading rows and lines
    def _hot_days(self) -> set:
        return set(self.live.db.days()) if getattr(self.live.db, "hot", False) else set()

    def _rows(self, s: datetime, T: datetime, plan, budget: Budget, scope=None):
        """Rows that may hold lines stamped in [s, T), newest ts_max first. Without archived days in the range: the stored (hot)
        rows. With them: day by day (rows sit in the file / archive of their ts_max day), each day's hot rows merged with its
        archived rows when no hot file holds that day any more (hot is authoritative for its days)."""
        cold = self.cold
        if cold is None or self.live.db is None:
            yield from self._hot_rows(s, T, plan, budget, scope)
            return
        from . import logtiers
        hot_days = self._hot_days()
        hi = T + timedelta(seconds=ROW_SPAN_S + 1)
        cdays = {d for d in cold.days() if d not in hot_days and logtiers.day_start(d) < hi and logtiers.day_end(d) > s}
        if not cdays:
            yield from self._hot_rows(s, T, plan, budget, scope)
            return
        sev_mask = plan.sev_mask if plan is not None else lq.ALL_SEV
        hosts = plan.hosts if plan is not None else None
        srcs = plan.sources if plan is not None else None
        allows = scope.allows if scope is not None else None
        prune = (lambda ch: not (int(ch.get("sev", lq.ALL_SEV)) & sev_mask)) if sev_mask != lq.ALL_SEV else None
        lt = "<" if T.microsecond == 0 else "<="

        def cold_rows(day):
            for r in cold.rows_desc(s, T, lt, None, prune, days={day}, pre=plan.prefilter if plan is not None else None):
                budget.cold += 1
                h = r.get("host") or ""
                sr = r.get("src") or ""
                if (not (int(r.get("sevmask") or lq.ALL_SEV) & sev_mask)
                        or (h and ((hosts is not None and lq.fold(h) not in hosts) or (allows is not None and not allows(h))))
                        or (sr and srcs is not None and lq.fold(sr) not in srcs)):
                    r["_skip"] = True
                yield r
        known = sorted(hot_days | set(cold.days()))
        floor = max(logtiers.day_start(logtiers.day_of(iso_s(s))), logtiers.day_start(known[0]) - timedelta(days=1))
        d = logtiers.day_start(logtiers.day_of(iso_s(hi)))
        key = lambda r: (r.get("ts_max") or "", r.get("ts_min") or "", int(r.get("id") or 0))   # noqa: E731
        while d >= floor:
            day = logtiers.day_key(d)
            hot = self._hot_rows(max(s, d), T, plan, budget, scope, ts_hi=d + timedelta(days=1))
            if day not in cdays:
                yield from hot
            else:
                last = None
                for r in heapq.merge(hot, cold_rows(day), key=key, reverse=True):
                    k = key(r)
                    if k != last:                                # a row both in a hot file and in the archive: once
                        last = k
                        yield r
            d -= timedelta(days=1)

    def _hot_rows(self, s: datetime, T: datetime, plan, budget: Budget, scope=None, ts_hi: Optional[datetime] = None):
        """Stored rows that may hold lines stamped in [s, T), newest ts_max first, keyset paged, pruned before decompression.
        ts_hi: only rows with ts_max before it (one day of a range that also reads the cold archive)."""
        db = self.live.db
        v1 = self._v1_present()
        params = [iso_s(s), iso_s(T)]
        lt = "<" if T.microsecond == 0 else "<="                # lines < T: a whole-second T excludes rows starting at T
        base = f"SELECT {ROW_COLS} FROM live_batches WHERE ts_max >= ? AND ts_min {lt} ?"   # index (ts_max, ts_min): no row fetch to test ts_min
        if ts_hi is not None:
            base += " AND ts_max < ?"
            params.append(iso_s(ts_hi))
        if not v1:
            base += " AND ts_max < ?"
            params.append(iso_s(T + timedelta(seconds=ROW_SPAN_S + 1)))
        host_in = self._host_in(plan)
        if host_in:
            base += f" AND host IN ({', '.join('?' * (len(host_in) + 1))})"
            params += host_in + [""]
        key = None
        sev_mask = plan.sev_mask if plan is not None else lq.ALL_SEV
        hosts = plan.hosts if plan is not None else None
        srcs = plan.sources if plan is not None else None
        allows = scope.allows if scope is not None else None
        while True:
            if key is None:
                sql, p = base, params
            else:
                sql = base + " AND ts_max <= ? AND (ts_max, ts_min, id) < (?, ?, ?)"
                p = params + [key[0], key[0], key[1], key[2]]
            rows = db._exec(sql + " ORDER BY ts_max DESC, ts_min DESC, id DESC LIMIT 200", tuple(p))
            if not rows:
                return
            for r in rows:                                     # a pruned row is still yielded (marked): the reader checks its budget
                if r.get("v") == 2:                            # and moves its watermark, but never decompresses it
                    h = r.get("host") or ""
                    sr = r.get("src") or ""
                    if (not (int(r.get("sevmask") or lq.ALL_SEV) & sev_mask)
                            or (h and ((hosts is not None and lq.fold(h) not in hosts) or (allows is not None and not allows(h))))
                            or (sr and srcs is not None and lq.fold(sr) not in srcs)):
                        r["_skip"] = True
                yield r
            if len(rows) < 200:
                return
            key = (rows[-1]["ts_max"], rows[-1]["ts_min"], rows[-1]["id"])

    def _host_in(self, plan):
        """Exact host names (as the counters know them) when the query pins a few hosts: rows are then found by the host index."""
        if plan is None or not plan.hosts or len(plan.hosts) > 50:
            return None
        known = {}
        for h in list(self.live.rollup.hosts):
            known.setdefault(lq.fold(h), []).append(h)
        out = []
        for f in plan.hosts:
            out += known.get(f, [f])
        return out[:200]

    def _row_lines(self, r: dict, s_us: int, T_us: int, upper, pre, ctx, budget: Budget):
        """(tsu, seq, view) for the row's lines stamped in [s, T) and below the cursor."""
        budget.rows += 1
        if r.get("_skip"):
            return
        text = row_bytes(r)
        if pre is not None and not pre.search(text):
            budget.lines += int(r.get("n") or 0) // 8              # skipped at C speed: a fraction of a decoded line each
            return
        rid = int(r["id"])
        try:
            recv = datetime.fromisoformat(r.get("received_at") or "").timestamp()
        except (TypeError, ValueError):
            recv = 0.0
        loads = json.loads
        for idx, ln in enumerate(text.split(b"\n")):
            if not ln or (pre is not None and not pre.search(ln)):
                continue
            budget.lines += 1
            try:
                d = loads(ln)
                tsu = _us(d["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if tsu < s_us or tsu >= T_us:
                continue
            q = d.get("q")
            seq = int(q) if q else -(rid * logstore.V1_ID_BASE + idx)
            if upper is not None and (tsu > upper[0] or (tsu == upper[0] and seq >= upper[1])):
                continue
            yield tsu, seq, DictLine(d, rid, idx, recv, tsu / 1e6, ctx)

    def _ring_views(self, s_us: int, T_us: int, ctx, upper=None) -> list:
        with self.live.lock:
            ring = list(self.live.buf)
        arr = self.live.arrival_of
        out = []
        for o in ring:
            v = ObsLine(o, ctx)
            tsu = round(v.ts * 1_000_000)
            if s_us <= tsu < T_us and (upper is None or tsu < upper[0] or (tsu == upper[0] and v.seq < upper[1])):
                v.received = arr(v.seq) or 0.0
                out.append((tsu, v.seq, v))
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return out

    def _slices(self, s: datetime, e: datetime, n_slices: int = 60) -> Optional[tuple]:
        """([(a, b)] newest first, k) when the stored range is expected to hold more than SAMPLE_LINES lines: n time slices, each
        1/k of its share of the range, counts scaled by k. None when the whole range is read."""
        if self.live.db is None:
            return None
        span = (e - s).total_seconds()
        if span < 120:
            return None
        est = self._expected_lines(s, e)
        if est <= SAMPLE_LINES:
            return None
        k = int(math.ceil(est / SAMPLE_LINES))
        n = max(1, min(n_slices, int(span // 60)))
        part = span / n
        out = []
        for i in range(n):
            a = s + timedelta(seconds=i * part)
            out.append((a, a + timedelta(seconds=part / k)))
        return list(reversed(out)), k

    def _expected_lines(self, s: datetime, e: datetime) -> float:
        """Lines stamped in [s, e): the live counters inside the kept hour, the per-minute history beyond it, else the arrival rate."""
        m0, m1 = self._mins(s, e)
        r = self.live.rollup
        if r.covers((m0, m1)):
            with r.lock:
                return float(sum(sum(c.values()) for m in range(m0, m1 + 1) for c in (r.cnt.get(m),) if c))
        if self.history is not None:
            try:
                rows = self.history.kb._exec("SELECT SUM(total) AS t FROM slo_minute WHERE minute >= ? AND minute <= ?",
                                             (s.strftime("%Y-%m-%dT%H:%M"), e.strftime("%Y-%m-%dT%H:%M")))
                if rows and rows[0]["t"] is not None:
                    return float(rows[0]["t"])
            except Exception:  # noqa: BLE001
                pass
        cold = self.cold
        if cold is not None and self.live.db is not None:          # archived days: the archive index knows its line counts
            lo = self._hot_lo()
            n = cold.lines_between(s, e, self._hot_days())
            if lo is None or e <= lo:
                return n
            return n + self._rate() * (e - max(s, lo)).total_seconds()
        return self._rate() * (e - s).total_seconds()

    def _rate(self) -> float:
        """Recent arrival rate (lines/s, last 5 minutes)."""
        now = time.time()
        with self.live.lock:
            rate = list(self.live._rate)
        if not rate:
            return 0.0
        span = min(300.0, max(1.0, now - rate[0][0]))
        return sum(k for t, k in rate if t > now - 300) / span

    def _selective(self, s: datetime, e: datetime, plan, pre, scope) -> bool:
        """Does the text prefilter pass few of the newest rows of the range (an id, a rare phrase)? Common terms survive a 1/k sample;
        rare ones do not."""
        seen = hit = 0
        for r in self._rows(s, e, plan, Budget(ms=150), scope):
            if r.get("_skip"):
                continue
            seen += 1
            hit += bool(pre.search(row_bytes(r)))
            if seen >= 48:
                break
        return seen == 0 or hit < 0.2 * seen

    def count_scan(self, s: datetime, e: datetime, plan, on_line, pre, scope, budget: Budget, ctx, sample: bool = True) -> dict:
        """Call on_line(view) for every in-range line in scope (newest rows first, order not guaranteed inside a row). Returns
        scan info: lines, rows, complete, frontier, stopped_by, sample."""
        info = {"lines": 0, "rows": 0, "complete": True, "frontier": iso_s(s), "stopped_by": None, "sample": 1}
        s_us, e_us = round(s.timestamp() * 1e6), round(e.timestamp() * 1e6)
        if self.live.db is None:
            for tsu, _q, v in self._ring_views(s_us, e_us, ctx):
                if scope is None or scope.allows(v.host):
                    on_line(v)
                    budget.lines += 1
            info["lines"] = budget.lines
            return info
        sl = self._slices(s, e) if sample else None
        if sl and pre is not None and self._selective(s, e, plan, pre, scope):
            sl = None                                           # a rare id or phrase is never sampled: it vanished from the chart, the
                                                                # facets and the groups while the list showed it (the prefilter keeps it fast)
        windows = sl[0] if sl else [(s, e)]
        if sl:
            info["sample"] = sl[1]
        stopped = None
        frontier = s
        for a, b in windows:
            a_us, b_us = round(a.timestamp() * 1e6), round(b.timestamp() * 1e6)
            for r in self._rows(a, b, plan, budget, scope):
                stopped = budget.over()
                if stopped:
                    frontier = min(b, datetime.fromisoformat(r["ts_max"]) + timedelta(seconds=1))
                    break
                for _t, _q, v in self._row_lines(r, a_us, b_us, None, pre, ctx, budget):
                    if scope is None or scope.allows(v.host):
                        on_line(v)
            if stopped:
                break
        info.update(lines=budget.lines, rows=budget.rows, ms=budget.ms())
        if budget.cold:
            info["cold"] = budget.cold
        if stopped:
            info.update(complete=False, frontier=iso_s(frontier), stopped_by=stopped)
        return info

    # ---------------------------------------------------------------- search
    def _cursor_hash(self, q: Q, s: datetime, e: datetime) -> str:
        return hashlib.sha1(f"{q.norm}\n{q.scope_key()}\n{s.isoformat()}\n{e.isoformat()}".encode()).hexdigest()[:10]

    @staticmethod
    def encode_cursor(t_us: int, seq: int, h: str) -> str:
        raw = json.dumps({"v": 1, "t": t_us, "s": seq, "h": h}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @staticmethod
    def decode_cursor(c: str) -> dict:
        try:
            d = json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))
            if d.get("v") != 1 or not isinstance(d.get("t"), int) or not isinstance(d.get("s"), int):
                raise ValueError
            return d
        except (ValueError, TypeError):
            raise BadCursor("bad_cursor")

    def search(self, text: str = "", last=None, start=None, end=None, limit: int = 100, cursor: Optional[str] = None, cols: str = "",
               scope=None, who=None) -> dict:
        t0 = time.perf_counter()
        q = self.query(text, scope)
        s, e, live = self.pin_range(last, start, end)
        h = self._cursor_hash(q, s, e)
        upper = None
        if cursor:
            c = self.decode_cursor(cursor)
            if c.get("h") != h:
                raise BadCursor("cursor_mismatch")
            upper = (c["t"], c["s"])
        limit = max(1, min(int(limit or 100), 500))
        colnames = [x.strip() for x in (cols or "").split(",") if x.strip()][:20]
        base = {"range": {"start": s.isoformat(), "end": e.isoformat(), "live": live},
                "query": {"text": q.text, "normalized": q.norm, "facet_only": q.plan.facet_only, "warnings": q.warnings},
                "retention": self.retention()}
        if self._expired(e):
            return {**base, "lines": [], "next": None, "total": {"value": 0, "exact": False, "source": "history"}, "scan": None,
                    "source": "history", "exact": False, "took_ms": int((time.perf_counter() - t0) * 1000)}
        key = ("search", q.norm, q.scope_key(), s.isoformat(), e.isoformat(), limit, cursor or "", ",".join(colnames))
        out = self._cached(key, lambda: self._search(q, s, e, live, upper, limit, colnames, h, who))
        return {**base, **out, "took_ms": int((time.perf_counter() - t0) * 1000)}

    def export_matches(self, text: str, start: str, end: str, scope, write, progress=None, cancel=None) -> dict:
        """The background audit search (System › Storage): every stored line in [start, end) that matches `text`, hot day files and
        cold archive alike, newest first, with no time budget (the explorer's own scan stops after 1.5 s a request); `write` gets
        each match as the explorer shows it (whole message) plus the stored line ("stored"). progress(fraction of the range
        scanned); cancel: a threading.Event."""
        q = self.query(text, scope)
        s, e, _live = self.pin_range(None, start, end)
        if self.live.db is None:
            raise ValueError("no stored lines here (the live ring only)")
        budget = Budget(ms=10 ** 12, lines=10 ** 15, rows=10 ** 15, cancel=cancel)
        s_us, e_us = round(s.timestamp() * 1e6), round(e.timestamp() * 1e6)
        span = max(1, e_us - s_us)
        n = rows = 0
        for r in self._rows(s, e, q.plan, budget, q.scope):
            if cancel is not None and cancel.is_set():
                break
            for _tsu, _seq, v in self._row_lines(r, s_us, e_us, None, q.plan.prefilter, q.ctx, budget):
                if q.pred(v):
                    o = self.line_obj(v)
                    o["message"] = v.msg or ""                  # whole, as stored (the explorer's list shortens it)
                    if getattr(v, "d", None) is not None:
                        o["stored"] = v.d                       # the stored line itself (what the archive holds)
                    write(o)
                    n += 1
            rows += 1
            if progress is not None and rows % 64 == 0:
                try:
                    progress(min(0.99, (e_us - _us(r["ts_max"])) / span))
                except (KeyError, TypeError, ValueError):
                    pass
        return {"matched": n, "lines_scanned": budget.lines, "rows": budget.rows, "cold_rows": budget.cold,
                "range": {"start": s.isoformat(), "end": e.isoformat()}, "query": q.norm, "cancelled": bool(cancel is not None and cancel.is_set())}

    def _search(self, q: Q, s, e, live, upper, limit, colnames, h, who) -> dict:
        ctx = q.ctx
        s_us, e_us = round(s.timestamp() * 1e6), round(e.timestamp() * 1e6)
        T = e if upper is None else min(e, datetime.fromtimestamp(upper[0] / 1e6, UTC) + timedelta(microseconds=1))
        emitted: list = []
        matched = 0
        info = {"lines": 0, "rows": 0, "ms": 0, "complete": True, "frontier": iso_s(s), "stopped_by": "range_start"}
        if self.live.db is None:
            views = self._ring_views(s_us, e_us, ctx, upper)
            for tsu, seq, v in views:
                if q.pred(v):
                    matched += 1
                    if len(emitted) < limit:
                        emitted.append((tsu, seq, v))
            info.update(lines=len(views), source="ring")
            full = len(emitted) >= limit and matched > limit
            nxt = self.encode_cursor(emitted[-1][0], emitted[-1][1], h) if full else None
            if full:
                info["stopped_by"] = "limit"
            total = {"value": matched, "exact": True, "source": "scan"}
            source = "ring"
        else:
            ev = self._scan_slot(who and (who, "search"))
            try:
                budget = Budget(cancel=ev)
                exact_counts = (q.plan.facet_only and self.cube_ok(s, e, live)) or (upper is None and self._group_count(q, s, e, live) is not None)
                emitted, matched, info, nxt_key = self._ordered(q, s, T, upper, limit, budget, count_more=upper is None and not exact_counts)
            finally:
                self._release(who and (who, "search"), ev)
            nxt = self.encode_cursor(nxt_key[0], nxt_key[1], h) if nxt_key else None
            total = self._total(q, s, e, live, matched, info, upper is not None)
            source = "cold" if info.get("cold") else "stored"
        lines = [self.line_obj(v, colnames) for _t, _s, v in emitted]
        exact = bool(total.get("exact"))
        return {"lines": lines, "next": nxt, "total": total, "scan": info, "source": source, "exact": exact}

    def _ordered(self, q: Q, s: datetime, T: datetime, upper, limit: int, budget: Budget, count_more: bool) -> tuple:
        """Lines matching q in [s, T) below the cursor, newest first by (ts, seq): rows are read by ts_max DESC; a buffered line is
        final once its stamp is at or above the next unread row's ts_max + 1 s (ts_max is stored to the second, truncated), so no
        unread row can hold a newer line. No arrival-order assumption (D1). After a full first page the scan keeps counting for
        COUNT_MS so the total is exact or a good estimate."""
        ctx = q.ctx
        s_us, T_us = round(s.timestamp() * 1e6), round(T.timestamp() * 1e6)
        pre = q.plan.prefilter
        heap: list = []
        emitted: list = []
        matched = 0
        full = exhausted = False
        stopped = None
        frontier_us = s_us
        tick = itertools.count()
        pred = q.pred
        count_until = None
        msec: dict = {}                                         # matches per second: the count above the frontier (estimates)
        for r in self._rows(s, T, q.plan, budget, q.scope):
            w_us = (_us(r["ts_max"]) // 1_000_000 + 1) * 1_000_000
            if not full:
                while heap and -heap[0][0] >= w_us:
                    x = heapq.heappop(heap)
                    emitted.append((-x[0], -x[1], x[3]))
                    if len(emitted) >= limit:
                        full = True
                        break
                if full:
                    if not count_more:
                        stopped = "limit"
                        frontier_us = emitted[-1][0]
                        break
                    count_until = time.perf_counter() + COUNT_MS / 1000
            why = budget.over()
            if not why and count_until is not None and time.perf_counter() > count_until:
                why = "budget_time"
            if why:
                stopped = why
                frontier_us = min(w_us, T_us)
                break
            for tsu, seq, v in self._row_lines(r, s_us, T_us, upper, pre, ctx, budget):
                if pred(v):
                    matched += 1
                    sec = tsu // 1_000_000
                    msec[sec] = msec.get(sec, 0) + 1
                    if not full:
                        heapq.heappush(heap, (-tsu, -seq, next(tick), v))
        else:
            exhausted = True                                    # nothing older is unread: everything buffered is final
            while heap and not full:
                x = heapq.heappop(heap)
                emitted.append((-x[0], -x[1], x[3]))
                if len(emitted) >= limit:
                    full = True
            frontier_us = s_us
        if full:
            more = (not exhausted) or bool(heap) or matched > len(emitted)
            nxt = (emitted[-1][0], emitted[-1][1]) if more else None
            stopped_by = "limit" if stopped in (None, "limit") else stopped
        elif exhausted:
            nxt, stopped_by = None, "range_start"
        else:                                                   # stopped by the budget before the page filled: continue below the
            nxt, stopped_by = (frontier_us, -(2 ** 62)), stopped   # frontier (every line at or above it was returned)
        info = {"lines": budget.lines, "rows": budget.rows, "ms": budget.ms(), "complete": exhausted,
                "frontier": _iso_ms(frontier_us / 1e6), "stopped_by": stopped_by}
        if budget.cold:
            info["cold"] = budget.cold
        if not exhausted:                                       # rows straddling the frontier were partly read: only the lines at or
            info["matched_above"] = sum(n for sec, n in msec.items() if sec * 1_000_000 >= frontier_us)   # above it were all seen
        return emitted, matched, info, nxt

    def _total(self, q: Q, s, e, live, matched, info, paged: bool) -> dict:
        if q.plan.facet_only and self.cube_ok(s, e, live):
            n, over = self.cube_count(q, s, e)
            return {"value": n, "exact": not over, "source": "counters", **({"fc_over": True} if over else {})}
        gc = self._group_count(q, s, e, live)
        if gc is not None:
            return gc
        if paged:
            return {"value": matched, "exact": False, "at_least": True, "below_cursor": True}
        if info.get("complete"):
            return {"value": matched, "exact": True, "source": "scan"}
        fp = lq.facet_part(q.node)
        try:
            fr = datetime.fromisoformat(info["frontier"])
        except (KeyError, TypeError, ValueError):
            fr = None
        if fr is not None and fr < e and self.cube_ok(s, e, live):
            # matches at or above the frontier, scaled by the facet part's lines over the whole range / over [frontier, end); the
            # minute holding the frontier counts in proportion (the counters are per minute: flooring it made a 1-minute range
            # look fully scanned and the estimate came out a third low)
            above = int(info.get("matched_above", matched))
            whole, _o = self.cube_count(q, s, e, fp)
            part = self._part_count(q, s, e, fr, fp)
            if part > 0:
                return {"value": max(matched, int(round(above * whole / part))), "exact": False, "estimated": True,
                        "basis": {"scanned": int(round(part)), "matched": above, "range_lines": whole}}
        return {"value": matched, "exact": False, "at_least": True}

    def _part_count(self, q: Q, s: datetime, e: datetime, fr: datetime, node) -> float:
        """Lines of `node` in [fr, e) by the per-minute counters; the minute holding fr counts in proportion."""
        m_lo = max(s, fr.replace(second=0, microsecond=0))
        m_hi = min(e, m_lo.replace(second=0, microsecond=0) + timedelta(minutes=1))
        part = float(self.cube_count(q, m_hi, e, node)[0]) if m_hi < e else 0.0
        span = (m_hi - m_lo).total_seconds()
        if span > 0:
            part += self.cube_count(q, m_lo, m_hi, node)[0] * max(0.0, (m_hi - fr).total_seconds()) / span
        return part

    def _coverage(self, q: Q, s, e, live, info) -> float:
        """How much the counts of a partial scan are scaled: lines in the range / lines at or above the scan frontier (the counters,
        team scope applied). 1 when the scan was complete or the counters do not cover the range. Facets and groups of a partial
        scan then estimate the whole range like the result count does, instead of showing the scanned minute next to it."""
        if not info or info.get("complete") or not self.cube_ok(s, e, live):
            return 1.0
        try:
            fr = datetime.fromisoformat(info["frontier"])
        except (KeyError, TypeError, ValueError):
            return 1.0
        if fr >= e:
            return 1.0
        whole = self.cube_count(q, s, e, lq.All())[0]
        part = self._part_count(q, s, e, fr, lq.All())
        return whole / part if part > 0 and whole > part else 1.0

    def _group_count(self, q: Q, s, e, live) -> Optional[dict]:
        """The exact total of `group:<key>` (with level / service / host / env / pool clauses, team scope included) from the host x
        group x level counters: the count the group row showed, so "lines of this pattern" lands on the same number. None when the
        query has anything else (text, attributes, file)."""
        if not q.plan.group_keys or not q.plan.conj or not self.cube_ok(s, e, live):
            return None
        keys, rest = None, []
        for it in lq.top_items(q.node):
            if isinstance(it, lq.Field) and it.name == "group" and it.op == "eq":
                ks = {v.text.lower() for v in it.values}
                keys = ks if keys is None else keys & ks
            elif all(isinstance(x, lq.Field) and not x.attr and x.name in ("level", "service", "host", "env", "pool") for x in lq._leaves(it)):
                rest.append(it)
            else:
                return None
        if not keys:
            return None
        r = self.live.rollup
        gids = [g for g in (r.group_id(k) for k in keys) if g is not None]
        kp = q.key_pred(rest[0] if len(rest) == 1 else lq.And(rest) if rest else lq.All())
        m0, m1 = self._mins(s, e)
        n = sum(c for sev, svc, host, c in r.group_lines(m0, m1, gids) if kp(sev, svc, "-", host)) if gids else 0
        return {"value": n, "exact": True, "source": "counters"}

    # ---------------------------------------------------------------- line objects
    def line_obj(self, v, cols=()) -> dict:
        msg = v.msg or ""
        m = logstore.scrub(msg) if len(msg) <= 200 else logstore.scrub_text(msg[:MSG_DETAIL])
        skew = None
        if v.received:
            d = v.received - v.ts
            if abs(d) > 120:
                skew = int(round(d))
        out = {"id": str(v.seq), "ts": _iso_ms(v.ts), "level": v.sev, "service": v.svc or "-", "host": v.host or "-", "env": v.env_name(),
               "pool": v.pool(), "source": v.src or "-", "message": m[:MSG_SHOW], "truncated": len(m) > MSG_SHOW or bool(getattr(v, "d", {}).get("mt")),
               "trace": v.trace() or None, "session": v.session() or None, "skew_s": skew}
        if cols:
            out["cols"] = {c: self._col(v, c) for c in cols}
        return out

    @staticmethod
    def _col(v, c: str):
        name, attr = lq.canonical_field(c)
        if attr:
            x = v.attr(name[1:])
            return x if x is None or isinstance(x, (int, float, bool)) else str(x)
        if name in lq.GETTERS:
            return lq.GETTERS[name](v)
        return {"level": v.sev, "message": None}.get(name)

    # ---------------------------------------------------------------- histogram
    def histogram(self, text: str = "", last=None, start=None, end=None, by: str = "level", top: int = 5, scope=None, who=None) -> dict:
        t0 = time.perf_counter()
        q = self.query(text, scope)
        s, e, live = self.pin_range(last, start, end)
        by = by if by in ("level", "service", "host", "source", "pool") else "level"
        top = max(1, min(int(top or 5), 10))
        key = ("hist", q.norm, q.scope_key(), s.isoformat(), e.isoformat(), by, top)
        out = self._cached(key, lambda: self._histogram(q, s, e, live, by, top, who))
        return {"range": {"start": s.isoformat(), "end": e.isoformat(), "live": live}, **out, "took_ms": int((time.perf_counter() - t0) * 1000)}

    def _steps(self, s, e) -> int:
        sec = (e - s).total_seconds()
        if sec >= 900:
            from .timerange import step_minutes
            return 60 * step_minutes(max(1, round(sec / 60)))
        return step_seconds(sec)

    def _histogram(self, q: Q, s, e, live, by, top, who) -> dict:
        step = self._steps(s, e)
        st = s.timestamp()
        nb = max(1, int(math.ceil((e.timestamp() - st) / step)))
        if self._expired(e) or (self._past_retention(e) and self._history_ok(q)):
            return self._hist_history(q, s, e, step, nb)        # older than the hot window: the long-term counters, when they answer
        valfn = self._by_fn(by, q.ctx)
        counts: list = [dict() for _ in range(nb)]
        totals: dict = {}
        exact, source, sample, info = True, "counters", 1, None
        if q.plan.facet_only and step >= 60 and self.cube_ok(s, e, live):
            m0, m1 = self._mins(s, e)
            r = self.live.rollup
            mins, over = r.cube(m0, m1)
            kp = q.key_pred()
            memo: dict = {}
            dec = r.cube_key
            for m, d in mins:
                b = int((m * 60 - st) // step)
                if b < 0 or b >= nb:
                    continue
                cb = counts[b]
                for k, c in d.items():
                    hit = memo.get(k)
                    if hit is None:
                        kk = dec(k)
                        hit = memo[k] = (kp(*kk), valfn(kk))
                    if hit[0]:
                        cb[hit[1]] = cb.get(hit[1], 0) + c
                        totals[hit[1]] = totals.get(hit[1], 0) + c
            exact = not over
        else:
            source = "stored" if self.live.db is not None else "ring"
            ev = self._scan_slot(who and (who, "histogram")) if self.live.db is not None else None
            try:
                budget = Budget(cancel=ev)
                pred = q.pred

                def on_line(v):
                    if pred(v):
                        b = int((v.ts - st) // step)
                        if 0 <= b < nb:
                            val = valfn((v.sev, v.svc or "-", v.src or "-", v.host or "-"))
                            cb = counts[b]
                            cb[val] = cb.get(val, 0) + 1
                            totals[val] = totals.get(val, 0) + 1
                info = self.count_scan(s, e, q.plan, on_line, q.plan.prefilter, q.scope, budget, q.ctx)
            finally:
                if ev is not None:
                    self._release(who and (who, "histogram"), ev)
            sample = info.get("sample", 1)
            exact = info["complete"] and sample == 1
            if info.get("cold"):
                source = "cold"
        if by == "level":
            keys = list(SEVS_DESC)
        else:
            ranked = sorted(totals.items(), key=lambda x: -x[1])
            keys = [k for k, _ in ranked[:top]] + (["_other"] if len(ranked) > top else [])
        keyset = set(keys)
        fr = datetime.fromisoformat(info["frontier"]).timestamp() if info and not info["complete"] else None
        buckets = []
        for i in range(nb):
            t = st + i * step
            v = {k: 0 for k in keys}
            for k, c in counts[i].items():
                kk = k if k in keyset else "_other"
                if kk in v:
                    v[kk] += c * sample
            buckets.append({"t": int(t), "total": sum(v.values()), "v": v, "scanned": fr is None or t >= fr})
        out = {"step_s": step, "by": by, "keys": keys, "buckets": buckets, "exact": exact, "source": source, "sample": sample}
        if info is not None:
            out["scan"] = info
        return out

    def _by_fn(self, by: str, ctx: Ctx):
        if by == "level":
            return lambda kk: kk[0]
        if by == "service":
            return lambda kk: kk[1]
        if by == "source":
            return lambda kk: kk[2]
        if by == "host":
            return lambda kk: kk[3]
        return lambda kk: ctx.pool_of(kk[3])

    def _hist_history(self, q: Q, s, e, step, nb) -> dict:
        """Older than the line retention: ERROR+ vs other per bar from slo_minute; only host / env / pool / level>=error apply."""
        out = {"step_s": max(step, 60), "by": "level", "keys": ["ERROR+", "OTHER"], "buckets": [], "exact": True, "source": "history", "sample": 1}
        unsupported = []
        err_only = False
        for leaf in lq._leaves(q.node):
            if isinstance(leaf, lq.Field) and not leaf.attr and leaf.name in ("host", "env", "pool"):
                continue
            if isinstance(leaf, lq.Field) and leaf.name == "level" and leaf.levels == frozenset({"ERROR", "CRITICAL"}):
                err_only = True
                continue
            unsupported.append(lq.normalize(leaf))
        if unsupported or self.history is None:
            out["unsupported"] = unsupported
            q.warnings.append(lq.warning("history_unsupported", leaf=", ".join(unsupported[:3])))
            out["warnings"] = q.warnings
            return out
        node = lq.And([x for x in lq.top_items(q.node) if not (isinstance(x, lq.Field) and x.name == "level")]) if err_only else q.node
        if isinstance(node, lq.And) and len(node.items) == 0:
            node = lq.All()
        kp = q.key_pred(node)
        step = max(step, 60)
        st = s.timestamp()
        nb = max(1, int(math.ceil((e.timestamp() - st) / step)))
        acc = [[0, 0] for _ in range(nb)]
        try:
            rows = self.history.kb._exec("SELECT minute, env, host, SUM(total) AS t, SUM(errors) AS er FROM slo_minute WHERE minute >= ? AND minute < ? GROUP BY minute, env, host",
                                         (s.strftime("%Y-%m-%dT%H:%M"), e.strftime("%Y-%m-%dT%H:%M")))
        except Exception:  # noqa: BLE001
            rows = []
        memo: dict = {}
        for r in rows:
            h = r["host"] or "-"
            ok = memo.get((h, r["env"]))
            if ok is None:
                ok = memo[(h, r["env"])] = kp("ERROR", "-", "-", h)
            if not ok:
                continue
            t = _dt(r["minute"] + ":00").timestamp()
            b = int((t - st) // step)
            if 0 <= b < nb:
                acc[b][0] += int(r["t"] or 0); acc[b][1] += int(r["er"] or 0)
        out["step_s"] = step
        out["buckets"] = [{"t": int(st + i * step), "total": a[1] if err_only else a[0], "v": {"ERROR+": a[1], "OTHER": 0 if err_only else a[0] - a[1]}, "scanned": True}
                          for i, a in enumerate(acc)]
        return out

    # ---------------------------------------------------------------- facets
    def facets(self, text: str = "", last=None, start=None, end=None, fields: str = "", limit: int = 12, search: str = "", scope=None, who=None) -> dict:
        t0 = time.perf_counter()
        q = self.query(text, scope)
        s, e, live = self.pin_range(last, start, end)
        want = [f.strip() for f in (fields or ",".join(FACET_FIELDS)).split(",") if f.strip() in FACET_FIELDS] or list(FACET_FIELDS)
        sf, stext = None, ""
        if search and ":" in search:
            sf, stext = search.split(":", 1)
            sf = lq.canonical_field(sf.strip())[0]
            if sf in FACET_FIELDS:
                want = [sf]
            stext = lq.fold(stext.strip())
        limit = max(1, min(int(limit or 12), 200))
        key = ("facets", q.norm, q.scope_key(), s.isoformat(), e.isoformat(), tuple(want), limit, sf or "", stext)
        out = self._cached(key, lambda: self._facets(q, s, e, live, want, limit, stext, who))
        return {"range": {"start": s.isoformat(), "end": e.isoformat(), "live": live}, **out, "took_ms": int((time.perf_counter() - t0) * 1000)}

    def _facets(self, q: Q, s, e, live, want, limit, stext, who) -> dict:
        mode = "multi" if q.plan.conj else "filtered"
        counts = {f: {} for f in want}
        if self._expired(e):
            return {"mode": mode, "facets": {f: self._facet_out(f, {}, q, limit, stext) for f in want}, "exact": False, "source": "history", "sample": 1}
        idx = {"level": 0, "service": 1, "source": 2, "host": 3}
        ctx = q.ctx

        def val(f, kk):
            if f in idx:
                return kk[idx[f]]
            if f == "env":
                return ctx.env_of(kk[3])
            return ctx.pool_of(kk[3])
        exact, source, sample, info = True, "counters", 1, None
        if q.plan.facet_only and self.cube_ok(s, e, live):
            m0, m1 = self._mins(s, e)
            r = self.live.rollup
            mins, over = r.cube(m0, m1)
            tot: dict = {}
            for _m, d in mins:
                for k, c in d.items():
                    tot[k] = tot.get(k, 0) + c
            dec = r.cube_key
            preds = {f: q.key_pred(lq.without_field(q.node, f) if mode == "multi" else None) for f in want}
            for k, c in tot.items():
                kk = dec(k)
                for f in want:
                    if preds[f](*kk):
                        v = val(f, kk)
                        cf = counts[f]
                        cf[v] = cf.get(v, 0) + c
            exact = not over
        else:
            items = lq.top_items(q.node) if mode == "multi" else [q.node]
            fns = [lq.compile(x, ctx) for x in items]
            touches = [{(l.name if isinstance(l, lq.Field) else l.target) for l in lq._leaves(x)} for x in items]
            need = {f: [i for i, t in enumerate(touches) if f not in t] if mode == "multi" else list(range(len(items))) for f in want}
            base = lq.And([x for x, t in zip(items, touches) if not (t & set(want))]) if mode == "multi" else q.node
            if isinstance(base, lq.And) and not base.items:
                base = lq.All()
            bplan = lq.plan(base)

            def on_line(v):
                bits = [f(v) for f in fns]
                kk = (v.sev, v.svc or "-", v.src or "-", v.host or "-")
                for f in want:
                    if all(bits[i] for i in need[f]):
                        x = val(f, kk) if f != "env" else v.env_name()
                        cf = counts[f]
                        cf[x] = cf.get(x, 0) + 1
            ev = self._scan_slot(who and (who, "facets")) if self.live.db is not None else None
            try:
                info = self.count_scan(s, e, bplan, on_line, bplan.prefilter, q.scope, Budget(cancel=ev), ctx)
            finally:
                if ev is not None:
                    self._release(who and (who, "facets"), ev)
            sample = info.get("sample", 1)
            scale = sample * self._coverage(q, s, e, live, info)
            if scale != 1:
                counts = {f: {k: int(round(n * scale)) for k, n in c.items()} for f, c in counts.items()}
            exact = info["complete"] and sample == 1
            source = "cold" if info.get("cold") else "stored" if self.live.db is not None else "ring"
        out = {"mode": mode, "facets": {f: self._facet_out(f, counts[f], q, limit, stext) for f in want}, "exact": exact, "source": source, "sample": sample,
               "scaled": round(scale / sample, 2) if info is not None and scale != sample else None}
        if info is not None:
            out["scan"] = info
        return out

    def _facet_out(self, f: str, c: dict, q: Q, limit: int, stext: str) -> dict:
        items = sorted(c.items(), key=lambda x: (-x[1], x[0]))
        if f == "level":
            items = sorted(c.items(), key=lambda x: SEVS_DESC.index(x[0]) if x[0] in SEVS_DESC else 9)
        if stext:
            items = [x for x in items if stext in lq.fold(str(x[0]))]
        top = q.plan.top_and.get(f, {"pos": [], "neg": []}) if q.plan.conj else {"pos": [], "neg": []}
        shown = items[:limit]
        return {"values": [{"v": k, "n": n} for k, n in shown], "distinct": len(items), "other": sum(n for _k, n in items[limit:]),
                "selected": list(top["pos"]), "excluded": list(top["neg"])}

    # ---------------------------------------------------------------- groups (group similar)
    def groups(self, text: str = "", last=None, start=None, end=None, limit: int = 100, scope=None, who=None) -> dict:
        t0 = time.perf_counter()
        q = self.query(text, scope)
        s, e, live = self.pin_range(last, start, end)
        limit = max(1, min(int(limit or 100), 500))
        key = ("groups", q.norm, q.scope_key(), s.isoformat(), e.isoformat(), limit)
        out = self._cached(key, lambda: self._groups(q, s, e, live, limit, who))
        return {"range": {"start": s.isoformat(), "end": e.isoformat(), "live": live}, **out, "took_ms": int((time.perf_counter() - t0) * 1000)}

    def _groups(self, q: Q, s, e, live, limit, who) -> dict:
        if self._expired(e):
            return {"groups": [], "distinct": 0, "total": 0, "exact": False, "source": "history", "sample": 1}
        # counters: exact per minute, host and line level (Rollup.gx) for level / service / host / env / pool queries and the team
        # scope, so a group's count equals the lines behind group:<key>; a file clause or text is scanned
        counters_ok = q.plan.facet_only and "source" not in q.plan.fields and self.cube_ok(s, e, live)
        if counters_ok:
            r = self.live.rollup
            m0, m1 = self._mins(s, e)
            kp = q.key_pred()
            res = r.groups_where((m0, m1), lambda sev, svc, host: kp(sev, svc, "-", host), True, limit)
            known = r.started < m0 * 60 - 300
            for g in res["groups"]:
                g["new"] = bool(known and g["first"] >= s.isoformat(timespec="seconds"))
                g["template"] = logstore.scrub_template(g["template"])
                g["sample"] = logstore.scrub(g["sample"]) if len(g["sample"]) <= 200 else privacy.scrub(g["sample"])
                g["sample_id"] = None
                g.pop("ref", None)
            return {**res, "exact": True, "source": "counters", "sample": 1}
        from .analysis import template_of
        acc: dict = {}
        cap = [0]
        pred = q.pred
        st = s.timestamp()
        span = max(1.0, e.timestamp() - st)

        def on_line(v):
            if cap[0] >= 200_000 or not pred(v):
                return
            cap[0] += 1
            tpl = template_of(v.msg or "")
            k = (tpl, v.svc or "-")
            g = acc.get(k)
            if g is None:
                g = acc[k] = {"count": 0, "level": v.sev, "hosts": set(), "first": v.ts, "last": v.ts, "sample": v, "trend": [0] * 30}
            g["count"] += 1
            if lq.SEV_RANK.get(v.sev, 1) > lq.SEV_RANK.get(g["level"], 1):
                g["level"] = v.sev
            if len(g["hosts"]) < 500:
                g["hosts"].add(v.host)
            if v.ts > g["last"]:
                g["last"], g["sample"] = v.ts, v
            if v.ts < g["first"]:
                g["first"] = v.ts
            g["trend"][min(29, max(0, int((v.ts - st) / span * 30)))] += 1
        ev = self._scan_slot(who and (who, "groups")) if self.live.db is not None else None
        try:
            info = self.count_scan(s, e, q.plan, on_line, q.plan.prefilter, q.scope, Budget(cancel=ev), q.ctx)
        finally:
            if ev is not None:
                self._release(who and (who, "groups"), ev)
        ks = info.get("sample", 1)
        cov = self._coverage(q, s, e, live, info)
        k = ks * cov
        rows = []
        for (tpl, svc), g in sorted(acc.items(), key=lambda x: -x[1]["count"])[:limit]:
            smp = g["sample"]
            rows.append({"key": logstore.group_key(tpl, svc), "template": logstore.scrub_template(tpl), "service": svc, "level": g["level"], "count": int(round(g["count"] * k)),
                         "hosts": len(g["hosts"]), "top_hosts": sorted(g["hosts"])[:5], "first": _iso_ms(g["first"]), "last": _iso_ms(g["last"]),
                         "trend": g["trend"], "sample_id": str(smp.seq), "sample": privacy.scrub(smp.msg or "")[:400], "new": False})
        return {"groups": rows, "distinct": len(acc), "total": int(round(sum(g["count"] for g in acc.values()) * k)), "exact": info["complete"] and k == 1 and cap[0] < 200_000,
                "source": "cold" if (info or {}).get("cold") else "stored" if self.live.db is not None else "ring", "sample": ks, "scaled": round(cov, 2) if cov != 1 else None, "scan": info}

    # ---------------------------------------------------------------- live tail
    def tail(self, text: str = "", after: Optional[int] = None, limit: int = 200, cols: str = "", scope=None, who=("", "")) -> dict:
        t0 = time.perf_counter()
        now = time.time()
        with self.lock:
            prev = self._polls.get(who)
            if prev is not None and now - prev < TAIL_MIN_GAP and after is not None:
                raise TooFast(int((TAIL_MIN_GAP - (now - prev)) * 1000) + 1)
            self._polls[who] = now
            if len(self._polls) > 5000:
                for k in [k for k, t in self._polls.items() if now - t > 600]:
                    self._polls.pop(k, None)
        q = self.query(text, scope)
        limit = max(1, min(int(limit or 200), TAIL_MAX))
        colnames = [x.strip() for x in (cols or "").split(",") if x.strip()][:20]
        live = self.live
        skipped = 0
        with live.lock:
            n = len(live.buf)
            last_seq = live.seq
            if after is None:
                ring = list(live.buf)
            else:
                first = live.buf[0].seq if n else last_seq + 1
                if after + 1 < first:
                    skipped = first - after - 1
                    ring = list(live.buf)
                else:
                    want = max(0, (live.buf[-1].seq if n else after) - after)
                    ring = list(itertools.islice(reversed(live.buf), min(n, want)))
                    ring.reverse()
        ctx = q.ctx
        pred = q.pred
        matched: list = []
        if after is None:
            for o in reversed(ring):
                v = ObsLine(o, ctx)
                if pred(v):
                    matched.append(v)
                    if len(matched) >= limit:
                        break
            cap = limit
            elapsed = None
        else:
            for o in ring:
                if o.seq <= after:
                    continue
                v = ObsLine(o, ctx)
                if pred(v):
                    matched.append(v)
            elapsed = max(0.5, now - prev) if prev is not None else 2.0
            cap = max(1, min(limit, TAIL_MAX, int(TAIL_CAP_S * elapsed)))
            matched.reverse()                                   # newest first
        shown = matched[:cap]
        for v in shown:
            v.received = live.arrival_of(v.seq) or 0.0
        nm = len(matched)
        rate = []
        with live.lock:
            for t, k in reversed(live._rate):                   # newest first: stop at 10 s back (the deque holds minutes)
                if t <= now - 10:
                    break
                rate.append(k)
        span = min(10.0, max(1.0, now - live.started))
        return {"lines": [self.line_obj(v, colnames) for v in shown], "after": last_seq, "matched": nm, "returned": len(shown),
                "dropped": max(0, nm - len(shown)) if after is not None else 0, "skipped": skipped,
                "rate": {"in_per_s": round(sum(rate) / span, 1), "match_per_s": round(nm / elapsed, 1) if elapsed else None},
                "cap_per_s": TAIL_CAP_S, "poll_ms": 2000, "query": {"normalized": q.norm, "warnings": q.warnings}, "source": "ring",
                "took_ms": int((time.perf_counter() - t0) * 1000)}

    # ---------------------------------------------------------------- one line: detail and context
    def _find(self, lid: int):
        """(view, row) for a line id: the ring first, else the stored row holding it. KeyError when unknown or expired."""
        live = self.live
        ctx = self.ctx()
        if lid > 0:
            with live.lock:
                buf = live.buf
                if buf and buf[0].seq <= lid <= buf[-1].seq:
                    i = lid - buf[0].seq
                    o = buf[i] if i < len(buf) and buf[i].seq == lid else None
                    if o is None:
                        import bisect
                        j = bisect.bisect_left(_SeqView(buf), lid)
                        o = buf[j] if j < len(buf) and buf[j].seq == lid else None
                else:
                    o = None
            if o is not None:
                v = ObsLine(o, ctx)
                v.received = live.arrival_of(lid) or 0.0
                return v, None
        if live.db is None:
            raise KeyError(str(lid))
        cold = self.cold
        floor = live.db.seq_floor() if cold is not None and lid > 0 and getattr(live.db, "hot", False) else None
        if floor is None or lid >= floor:                       # below every hot line id: archived, skip the hot pages
            try:
                return self._find_stored(lid, ctx)
            except KeyError:
                if cold is None:
                    raise
        hd = self._hot_days()                                   # the cold archive: the days no hot file holds
        hit = self._in_rows(cold.find_seq(lid, hd) if lid > 0 else cold.find_id((-lid) // logstore.V1_ID_BASE, hd), lid, ctx)
        if hit is None:
            raise KeyError(str(lid))
        return hit

    def _find_stored(self, lid: int, ctx):
        live = self.live
        if lid > 0:
            if not live._v2:
                raise KeyError(str(lid))
            # the row holding lid has seq_min <= lid, and only rows of its own POST rank above it by seq_min (an earlier POST's ids are
            # all lower, a later POST's all higher): it is among the first rows by seq_min DESC, a few pages when a POST spans many minutes
            below = lid + 1
            for _page in range(64):
                got = live.db._exec(f"SELECT {ROW_COLS} FROM live_batches WHERE seq_min < ? AND seq_min > 0 ORDER BY seq_min DESC LIMIT 32", (below,))
                hit = self._in_rows([r for r in got if int(r.get("seq_max") or 0) >= lid], lid, ctx)
                if hit is not None:
                    return hit
                if len(got) < 32:
                    break
                below = int(got[-1]["seq_min"])
            raise KeyError(str(lid))
        rid = (-lid) // logstore.V1_ID_BASE
        rows = live.db._exec(f"SELECT {ROW_COLS if live._v2 else 'id, ts_min, ts_max, received_at, agent, env, n, payload'} FROM live_batches WHERE id = ?", (rid,))
        hit = self._in_rows(rows, lid, ctx)
        if hit is None:
            raise KeyError(str(lid))
        return hit

    @staticmethod
    def _in_rows(rows: list, lid: int, ctx):
        for r in rows:
            try:
                recv = datetime.fromisoformat(r.get("received_at") or "").timestamp()
            except (TypeError, ValueError):
                recv = 0.0
            for idx, ln in enumerate(row_bytes(r).split(b"\n")):
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if logstore.line_id(d, int(r["id"]), idx) == lid:
                    return DictLine(d, int(r["id"]), idx, recv, _us(d["ts"]) / 1e6, ctx), r
        return None

    def line(self, lid: int, scope=None) -> dict:
        v, row = self._find(int(lid))
        if scope is not None and not scope.allows(v.host):
            raise KeyError(str(lid))                            # 404, not 403: existence is not confirmed
        p = v.packed() if isinstance(v, ObsLine) else v.d
        msg = p.get("msg") or v.msg or ""
        shown = logstore.scrub_text(msg[:MSG_DETAIL])
        skew = None
        if v.received:
            d = v.received - v.ts
            if abs(d) > 120:
                skew = int(round(d))
        tr = v.trace()
        trace = None
        if tr:
            stored = False
            try:
                stored = bool(self.kb is not None and self.kb._exec("SELECT 1 AS x FROM spans WHERE trace_id=? LIMIT 1", (tr,)))
            except Exception:  # noqa: BLE001
                stored = False
            trace = {"id": tr, "stored": stored, "link": f"/traces/{tr}"}
        ses = v.ses()
        session = None
        if ses:
            from .rollup import session_link
            info = tuple(ses) + ("",) * (6 - len(ses))
            session = {"id": info[0], "kind": info[1], "user": info[2], "client": info[3], "rum": info[4] or None,
                       "link": session_link(info, info[5]) or (f"/traces?session={info[0]}" if info[0] else "")}
        from .analysis import template_of
        tpl = template_of(v.msg or "")
        gk = logstore.group_key(tpl, v.svc or "-")
        gid = self.live.rollup.group_id(gk)
        count_1h = self.live.rollup.group_counts(gid, 60) if gid is not None else None
        t = datetime.fromtimestamp(v.ts, UTC)
        a, b = (t - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), (t + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_state = v.raw_state()
        at = p.get("at") or {}
        return {"id": str(v.seq), "ts": _iso_ms(v.ts), "received": _iso_ms(v.received) if v.received else None, "skew_s": skew,
                "level": v.sev, "service": v.svc or "-", "host": v.host or "-", "env": v.env_name(), "pool": v.pool(), "source": v.src or "-",
                "line_no": v.ln or None, "agent": v.agent, "kind": v.kind, "origin": v.org, "parser": p.get("prs") or (v.o.parser if isinstance(v, ObsLine) else ""),
                "message": shown, "message_truncated": bool(p.get("mt")) or len(msg) > MSG_DETAIL,
                "raw": p.get("raw") if raw_state == "stored" else None, "raw_state": raw_state,
                "attrs": at, "attrs_dropped": int(p.get("atx") or 0), "trace": trace, "session": session,
                "host_link": {"ops": f"/ops?host={v.host}&start={a}&end={b}"} if v.host else None,
                "group": {"key": gk, "template": logstore.scrub_template(tpl), "count_1h": count_1h}, "source_of_detail": "ring" if isinstance(v, ObsLine) else "cold" if (row or {}).get("_cold") else "stored"}

    def context(self, lid: int, before: int = 20, after: int = 20, text: str = "", scope=None) -> dict:
        v, _row = self._find(int(lid))
        if scope is not None and not scope.allows(v.host):
            raise KeyError(str(lid))
        before, after = max(0, min(int(before), 200)), max(0, min(int(after), 200))
        ctx = self.ctx()
        pred = None
        if text:
            pred = self.query(text, scope).pred
        lo, hi = v.ts - CONTEXT_S, v.ts + CONTEXT_S
        host, src = v.host, v.src
        items: list = []
        if self.live.db is not None and self.live._v2 or (self.live.db is not None and lid < 0):
            rows = self.live.db._exec(f"SELECT {ROW_COLS} FROM live_batches WHERE agent = ? AND ts_max >= ? AND ts_min <= ? ORDER BY ts_max",
                                      (v.agent if v.agent != "-" else "", iso_s(datetime.fromtimestamp(lo, UTC)), iso_s(datetime.fromtimestamp(hi, UTC))))
            if lid > 0 and not any(int(r.get("seq_min") or 0) <= lid <= int(r.get("seq_max") or 0) for r in rows):
                # the anchor's own row is bounded by its arrival (a stale stamp, logstore.encode_rows): read it by line id
                rows += [r for r in self.live.db._exec(f"SELECT {ROW_COLS} FROM live_batches WHERE seq_min <= ? AND seq_min > 0 ORDER BY seq_min DESC LIMIT 32",
                                                       (lid,)) if int(r.get("seq_max") or 0) >= lid and (r.get("agent") or "") == (v.agent if v.agent != "-" else "")]
            cold = self.cold
            if cold is not None:                                 # archived days of the window (those no hot file holds)
                hd = self._hot_days()
                rows += cold.rows_agent(v.agent if v.agent != "-" else "", datetime.fromtimestamp(lo, UTC), datetime.fromtimestamp(hi, UTC), hd)
                if lid > 0 and not any(int(r.get("seq_min") or 0) <= lid <= int(r.get("seq_max") or 0) for r in rows):
                    rows += [r for r in cold.find_seq(lid, hd) if (r.get("agent") or "") == (v.agent if v.agent != "-" else "")]
            for r in rows:
                if (r.get("host") and r["host"] != host) or (r.get("src") and r["src"] != src):
                    continue
                try:
                    recv = datetime.fromisoformat(r.get("received_at") or "").timestamp()
                except (TypeError, ValueError):
                    recv = 0.0
                for idx, ln in enumerate(row_bytes(r).split(b"\n")):
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if (d.get("host") or "") != host or (d.get("src") or "") != src:
                        continue
                    tsu = _us(d["ts"])
                    if lo * 1e6 <= tsu <= hi * 1e6:
                        items.append(DictLine(d, int(r["id"]), idx, recv, tsu / 1e6, ctx))
        else:
            with self.live.lock:
                ring = list(self.live.buf)
            for o in ring:
                if (o.host or "") == host and (o.source or "") == src:
                    x = ObsLine(o, ctx)
                    if lo <= x.ts <= hi:
                        items.append(x)
        items.sort(key=lambda x: (x.ts, x.ln or 0, x.seq))
        pos = next((i for i, x in enumerate(items) if x.seq == v.seq), None)
        if pos is None:
            items.append(v); items.sort(key=lambda x: (x.ts, x.ln or 0, x.seq))
            pos = next(i for i, x in enumerate(items) if x.seq == v.seq)
        b_items = items[max(0, pos - before):pos]
        a_items = items[pos + 1:pos + 1 + after]

        def obj(x):
            d = self.line_obj(x)
            d["line_no"] = x.ln or None
            d["match"] = bool(pred(x)) if pred is not None else False
            return d
        return {"anchor": str(v.seq), "host": host or "-", "source": src or "-", "before": [obj(x) for x in b_items], "after": [obj(x) for x in a_items],
                "more_before": pos - before > 0, "more_after": pos + 1 + after < len(items), "window_s": CONTEXT_S}

    # ---------------------------------------------------------------- helpers for the query bar
    def parse(self, text: str, scope=None) -> dict:
        q = self.query(text, scope)
        return {"ok": True, "normalized": q.norm, "facet_only": q.plan.facet_only, "leaves": q.plan.leaves, "warnings": q.warnings}

    def _attr_warnings(self, q: Q, scope) -> None:
        if not q.plan.attrs:
            return
        seen = {a["name"].lower() for a in self.live.rollup.attr_keys(None if scope is None else scope.allows)}
        for leaf in lq._leaves(q.node):
            if isinstance(leaf, lq.Field) and leaf.attr and not leaf.typed.startswith("@") and leaf.name[1:].lower() not in seen:
                sug = _closest(leaf.typed.lower(), lq.BUILTIN + tuple(lq.ALIASES))
                q.warnings.insert(0, lq.warning("unknown_field", f=leaf.typed, s=sug or "", hint=f' "{sug}" mi demek istediniz?' if sug else "",
                                                hint_en=f' Did you mean "{sug}"?' if sug else ""))

    def rewrite(self, text: str, op: str, field: str, value: str) -> dict:
        out = lq.rewrite(text or "", op, field, value)
        return {"q": out, "normalized": lq.normalize(lq.parse(out).node)}

    def fields(self, scope=None) -> dict:
        out = [{"name": "level", "kind": "facet", "values": ["error", "warn", "info", "debug", "critical"]}]
        out += [{"name": f, "kind": "facet"} for f in ("service", "host", "env", "pool", "source")]
        out += [{"name": f, "kind": "number"} for f in ("duration_ms", "line")]
        out += [{"name": f, "kind": "field"} for f in ("agent", "kind", "origin", "trace", "session", "user", "client", "message", "raw", "group")]
        if self.app_names is not None:                 # app:<name> -> the application's hosts and services (watchover.applications)
            try:
                out.append({"name": "app", "kind": "app", "values": self.app_names(scope)[:500]})
            except Exception:  # noqa: BLE001
                pass
        for a in self.live.rollup.attr_keys(None if scope is None else scope.allows)[:500]:
            out.append({"name": "@" + a["name"], "kind": "attr", "type": a["type"], "seen": a["seen"]})
        return {"fields": out, "operators": ["AND", "OR", "NOT", "-"]}

    # ---------------------------------------------------------------- saved searches
    def _name(self, email: str) -> str:
        try:
            u = self.users.get(email) if self.users is not None else None
            return (u or {}).get("name") or email.split("@")[0]
        except Exception:  # noqa: BLE001
            return email.split("@")[0]

    def saved_list(self, email: str) -> list:
        rows = self.kb._exec("SELECT * FROM log_searches WHERE owner = ? OR shared = 1 ORDER BY CASE WHEN owner = ? THEN 0 ELSE 1 END, name", (email, email))
        return [{"id": r["id"], "name": r["name"], "query": r["query"], "range": r["range"] or "", "columns": json.loads(r["columns"] or "[]"),
                 "group_similar": bool(r["group_similar"]), "shared": bool(r["shared"]), "owner": self._name(r["owner"]), "mine": r["owner"] == email,
                 "updated_at": r["updated_at"]} for r in rows]

    def saved_get(self, sid: int) -> dict:
        rows = self.kb._exec("SELECT * FROM log_searches WHERE id = ?", (int(sid),))
        if not rows:
            raise KeyError(str(sid))
        return rows[0]

    def saved_create(self, email: str, d: dict) -> int:
        name = (d.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("name required")
        lq.parse(d.get("query") or "")
        if self.kb._exec("SELECT 1 AS x FROM log_searches WHERE owner = ? AND name = ?", (email, name)):
            raise FileExistsError(name)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return self.kb._insert("INSERT INTO log_searches (owner, name, query, range, columns, group_similar, shared, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (email, name, d.get("query") or "", (d.get("range") or "")[:200], json.dumps(list(d.get("columns") or [])[:40]),
                                int(bool(d.get("group_similar"))), int(bool(d.get("shared"))), now, now))

    def saved_update(self, sid: int, email: str, is_admin: bool, d: dict) -> None:
        r = self.saved_get(sid)
        if r["owner"] != email and not is_admin:
            raise PermissionError("owner only")
        f = {}
        if "name" in d and d["name"] is not None:
            name = d["name"].strip()[:120]
            if not name:
                raise ValueError("name required")
            if name != r["name"] and self.kb._exec("SELECT 1 AS x FROM log_searches WHERE owner = ? AND name = ?", (r["owner"], name)):
                raise FileExistsError(name)
            f["name"] = name
        if d.get("query") is not None:
            lq.parse(d["query"])
            f["query"] = d["query"]
        if d.get("range") is not None:
            f["range"] = d["range"][:200]
        if d.get("columns") is not None:
            f["columns"] = json.dumps(list(d["columns"])[:40])
        for k in ("group_similar", "shared"):
            if d.get(k) is not None:
                f[k] = int(bool(d[k]))
        if f:
            f["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self.kb._exec(f"UPDATE log_searches SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", tuple(f.values()) + (int(sid),))

    def saved_delete(self, sid: int, email: str, is_admin: bool) -> None:
        r = self.saved_get(sid)
        if r["owner"] != email and not is_admin:
            raise PermissionError("owner only")
        self.kb._exec("DELETE FROM log_searches WHERE id = ?", (int(sid),))


class _SeqView:
    """A read-only sequence of the ring's seqs for bisect (the ring is in seq order)."""
    def __init__(self, buf):
        self.buf = buf

    def __len__(self):
        return len(self.buf)

    def __getitem__(self, i):
        return self.buf[i].seq


def _closest(word: str, names) -> Optional[str]:
    best, bd = None, 3
    for n in names:
        d = _lev(word, n)
        if d < bd:
            best, bd = n, d
    return best if bd <= 2 else None


def _lev(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
