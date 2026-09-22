"""Live ingest: agents POST events to a small HTTP receiver; the dashboard charts them as they arrive.

    store = LiveStore(spool="data/live/events.jsonl")
    start_receiver(store, port=8600, api_key="secret")     # daemon thread inside the Streamlit process
    POST /ingest  (X-API-Key or Authorization: Bearer)   body: JSON array | JSONL | CSV | syslog | any text
    GET  /health

Any format the pipeline parses is accepted, so the agent can ship raw log lines. Standard library only.
"""

from __future__ import annotations

import hmac
import json
import re
import shutil
import statistics
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .models import SEV_RANK, Observation
from .pipeline import ingest_bytes
from . import scenario

UTC = timezone.utc


def host_match(h: str, sel) -> bool:
    """Scope filter: sel is one host name or a collection of names (multi-select: two servers averaged together)."""
    return h == sel if isinstance(sel, str) else h in sel
METRIC_KEYS = {"cpu": "cpu", "cpu_percent": "cpu", "cpu_pct": "cpu", "memory": "memory", "mem": "memory", "mem_percent": "memory",
               "memory_percent": "memory", "disk": "disk", "disk_percent": "disk", "disk_pct": "disk",
               "gpu": "gpu", "gpu_percent": "gpu", "gpu_util": "gpu", "gpu_utilization": "gpu"}
METRICS = ("cpu", "gpu", "memory", "disk")
ENV_ORDER = ["prod", "staging", "test", "qa", "dev", "unknown"]   # stable tile order on the operations page
LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s?ms\b")


class LiveStore:
    """Thread-safe ring buffer of recent observations + append-only JSONL spool on disk."""

    def __init__(self, spool: str | Path | None = None, maxlen: int = 50_000):
        self._memo: dict = {}
        self.buf: deque[Observation] = deque(maxlen=maxlen)
        self.metrics: deque[tuple] = deque(maxlen=maxlen)     # (ts, host, metric, value, env)
        self.lock = threading.Lock()
        self.spool = Path(spool) if spool else None
        self.received = 0
        self.agents: dict[str, float] = {}      # agent name -> last seen epoch
        self.on_ingest = None                    # optional hook(events): service-level history rollups
        self.enricher = None                     # optional hook(observation) -> bool: inventory match (IP → hostname, dc, criticality)
        self.metric_hosts: dict[str, set] = {}   # sender -> hosts it reported metrics for (purge on simulation off)
        self.matched = 0                         # events matched to the inventory
        self.started = time.time()
        self.db = None                           # attach_db(): the durable live window (live_batches) behind the ring buffer
        self.retention_h = 48                    # how long batches stay in the database
        self.window_max_events = 200_000         # a window read from the database is capped to the newest N events
        self.spool_max_bytes = self.SPOOL_MAX_BYTES
        self._rate: deque[tuple[float, int]] = deque(maxlen=60_000)  # (epoch, events) per ingest; 5 min at 200 batches/s
        self._persist_n = 0
        self.db_served = 0                       # windows answered from the database instead of the ring
        self.last_db_window: tuple | None = None
        self.last_db_window_ms = 0.0
        self._ingest_ms: deque[float] = deque(maxlen=500)   # per-batch ingest time (parse + ring + hooks + db)
        self._detect: dict[tuple[str, str], dict] = {}       # (agent, file) -> format/family decided once; re-checked every 200 batches
        if self.spool:
            self.spool.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- durable window (100+ hosts: the ring alone is seconds of data)
    def attach_db(self, db) -> None:
        """Keep every ingested batch in `live_batches` so a window longer than the ring (anomaly scan, live analysis, learner)
        is complete whatever the event rate. One row per agent POST, JSONL payload, pruned after `retention_h` hours."""
        db._exec(f"""CREATE TABLE IF NOT EXISTS live_batches (id {db.pk}, ts_min TEXT, ts_max TEXT, received_at TEXT, agent TEXT DEFAULT '',
            env TEXT DEFAULT '', n INTEGER DEFAULT 0, payload TEXT DEFAULT '')""")
        db._exec("CREATE INDEX IF NOT EXISTS live_batches_ts ON live_batches(ts_max)")
        self.db = db

    @staticmethod
    def _utc_iso(ts: datetime) -> str:
        return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _pack(o: Observation, agent: str) -> dict:
        a = o.attributes or {}
        d = {"ts": o.timestamp.isoformat(), "sev": o.severity, "svc": o.service, "host": o.host, "env": o.environment, "msg": o.message,
             "src": o.source, "agent": a.get("agent", agent), "kind": o.kind}
        if o.origin:
            d["org"] = o.origin
        if a.get("site"):
            d["site"] = a["site"]
        if a.get("agent_id") is not None:
            d["aid"] = a["agent_id"]
        return d

    @staticmethod
    def _unpack(d: dict) -> Observation:
        attrs = {"agent": d.get("agent", "-")}
        if d.get("site"):
            attrs["site"] = d["site"]
        if d.get("aid") is not None:
            attrs["agent_id"] = d["aid"]
        ts = datetime.fromisoformat(d["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return Observation(timestamp=ts, message=d.get("msg", ""), kind=d.get("kind", "log"), severity=d.get("sev", "INFO"), service=d.get("svc", ""),
                           host=d.get("host", ""), environment=d.get("env", ""), origin=d.get("org", ""), attributes=attrs, source=d.get("src", ""))

    def _persist(self, events: list[Observation], agent: str, env: str) -> None:
        if not self.db or not events:
            return
        try:
            tss = [self._utc_iso(o.timestamp) for o in events]
            payload = "\n".join(json.dumps(self._pack(o, agent), ensure_ascii=False) for o in events)
            self.db._insert("INSERT INTO live_batches (ts_min, ts_max, received_at, agent, env, n, payload) VALUES (?,?,?,?,?,?,?)",
                            (min(tss), max(tss), self._utc_iso(datetime.now(UTC)), agent, env or "", len(events), payload))
            self._persist_n += 1
            if self._persist_n % 200 == 0:
                self.prune()
        except Exception:  # noqa: BLE001 - the database is never allowed to stop ingestion
            pass

    def prune(self, now: datetime | None = None) -> int:
        """Drop batches older than `retention_h`; returns the number removed (0 when no database)."""
        if not self.db:
            return 0
        cut = self._utc_iso((now or datetime.now(UTC)) - timedelta(hours=float(self.retention_h)))
        before = self.db._exec("SELECT COUNT(*) AS n FROM live_batches WHERE ts_max < ?", (cut,))
        self.db._exec("DELETE FROM live_batches WHERE ts_max < ?", (cut,))
        return int(before[0]["n"]) if before else 0

    def _window_from_db(self, since: datetime) -> list[Observation]:
        t0 = time.perf_counter()
        try:
            return self._window_from_db_inner(since)
        finally:
            self.last_db_window_ms = round((time.perf_counter() - t0) * 1000, 1)

    def _window_from_db_inner(self, since: datetime) -> list[Observation]:
        rows = self.db._exec("SELECT payload FROM live_batches WHERE ts_max >= ? ORDER BY id", (self._utc_iso(since),))
        s_utc = since if since.tzinfo else since.replace(tzinfo=UTC)
        out: list[Observation] = []
        for r in rows:
            for line in (r["payload"] or "").split("\n"):
                if not line:
                    continue
                try:
                    o = self._unpack(json.loads(line))
                except (ValueError, KeyError):
                    continue
                if o.timestamp >= s_utc:
                    out.append(o)
        if len(out) > self.window_max_events:
            out = out[-self.window_max_events:]
        self.db_served += 1
        self.last_db_window = (self._utc_iso(since), len(out))
        return out

    def db_stats(self) -> dict:
        if not self.db:
            return {"enabled": False}
        try:
            r = self.db._exec("SELECT COUNT(*) AS batches, COALESCE(SUM(n), 0) AS events, MIN(ts_min) AS oldest, MAX(ts_max) AS newest, COALESCE(SUM(LENGTH(payload)), 0) AS bytes FROM live_batches")[0]
            return {"enabled": True, "batches": int(r["batches"] or 0), "events": int(r["events"] or 0), "oldest": r["oldest"] or "", "newest": r["newest"] or "", "bytes": int(r["bytes"] or 0)}
        except Exception:  # noqa: BLE001
            return {"enabled": True, "error": True}

    def capacity(self, window_min: int = 5) -> dict:
        """What the operator needs to size the box: events/s, how much of the anomaly window the ring still covers, database
        window depth, spool and disk. `warnings` are the things that will bite first at 100+ hosts."""
        now = time.time()
        with self.lock:
            rate = list(self._rate); n = len(self.buf); maxlen = self.buf.maxlen or 1
            head = [self.buf[i].timestamp for i in range(min(n, 500))]                      # arrival is only roughly ordered under 100 agents:
            tail = [self.buf[-1 - i].timestamp for i in range(min(n, 500))]                 # take the extremes of both ends, not two single events
        norm = lambda t: t if t.tzinfo else t.replace(tzinfo=UTC)
        oldest = min(map(norm, head)) if head else None; newest = max(map(norm, tail)) if tail else None
        window60 = min(60.0, max(1.0, now - rate[0][0])) if rate else 60.0                   # a young process: divide by the seconds actually observed
        r60 = sum(k for t, k in rate if t > now - 60) / window60
        r300 = sum(k for t, k in rate if t > now - 300) / min(300.0, max(1.0, now - rate[0][0])) if rate else 0.0
        span = max(0.0, (newest - oldest).total_seconds()) if (oldest and newest) else 0.0
        need = window_min * 60
        spool_bytes = 0; disk = None
        if self.spool:
            try:
                for f in self.spool.parent.glob(self.spool.name + "*"):
                    spool_bytes += f.stat().st_size
                u = shutil.disk_usage(self.spool.parent)
                disk = {"total": u.total, "free": u.free, "free_pct": round(100.0 * u.free / u.total, 1) if u.total else 0}
            except OSError:
                pass
        db = self.db_stats()
        warnings = []
        if n >= maxlen and span < need:
            warnings.append({"code": "ring_short", "detail": f"ring holds {span:.0f}s at this rate, the {window_min} min window is served from the database"} if db.get("enabled")
                            else {"code": "ring_short_no_db", "detail": f"ring holds {span:.0f}s, no database window: the {window_min} min analysis is incomplete"})
        if disk and disk["free_pct"] < 10:
            warnings.append({"code": "disk_low", "detail": f"{disk['free_pct']}% disk free"})
        if r60 > 0 and self.db and db.get("bytes", 0) and db.get("events"):
            per_day = r60 * 86400 * (db["bytes"] / max(1, db["events"]))
            if per_day > 50 * 1024 ** 3:
                warnings.append({"code": "db_growth", "detail": f"~{per_day / 1024 ** 3:.0f} GB/day of live batches at the current rate; lower live_retention_h"})
        ing = list(self._ingest_ms)
        ing_stats = {"batches": len(ing), "avg_ms": round(sum(ing) / len(ing), 2) if ing else 0.0, "p95_ms": round(sorted(ing)[int(len(ing) * 0.95) - 1], 2) if len(ing) >= 20 else 0.0}
        return {"events_per_s": round(r60, 2), "events_per_s_5m": round(r300, 2), "ingest": ing_stats, "last_db_window_ms": self.last_db_window_ms, "ring": {"events": n, "maxlen": maxlen, "fill_pct": round(100.0 * n / maxlen, 1), "span_s": round(span),
                "covers_window": bool(span >= need or n < maxlen)}, "window_min": window_min, "db": db, "db_served": self.db_served, "last_db_window": self.last_db_window,
                "spool_bytes": spool_bytes, "disk": disk, "retention_h": self.retention_h, "window_max_events": self.window_max_events, "warnings": warnings}

    def ingest(self, name: str, data: bytes, agent: str = "unknown", env: str = "", site: str = "", agent_id: int | None = None) -> int:
        t0 = time.perf_counter()
        try:
            return self._ingest(name, data, agent, env, site, agent_id)
        finally:
            self._ingest_ms.append((time.perf_counter() - t0) * 1000)

    def _ingest(self, name: str, data: bytes, agent: str, env: str, site: str, agent_id: int | None) -> int:
        key = (agent, name)
        hint = self._detect.get(key)
        if hint is not None:
            hint["n"] += 1
            if hint["n"] % 200 == 0:                          # a stream can change shape: re-detect now and then
                hint = None
        obs, rep = ingest_bytes(name, data, hint=hint)
        if rep and rep[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
            self._detect[key] = {"format": rep[0]["format"], "confidence": rep[0].get("confidence", 0.9), "family": rep[0].get("family", ""), "n": 1}
            if len(self._detect) > 5000:
                self._detect.clear()
        now = datetime.now(UTC)
        metric_rows: list[tuple] = []
        events: list[Observation] = []
        horizon = now + timedelta(days=1)
        for o in obs:
            ts = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)
            if o.attributes.pop("_no_ts", False) or o.timestamp.year < 2000 or ts > horizon:   # missing, ancient or absurdly future stamp: arrival time
                o.timestamp = now
            o.attributes["agent"] = agent
            if self.enricher is not None:
                try:
                    if self.enricher(o):
                        self.matched += 1
                except Exception:  # noqa: BLE001
                    pass
            if env:                                          # the registered environment wins over guessing from host names
                o.environment = env
            if site:
                o.attributes["site"] = site
            if agent_id is not None:
                o.attributes["agent_id"] = agent_id
            m = metric_of(o)
            if m:
                metric_rows.extend(m)
            else:
                events.append(o)
        with self.lock:
            self.buf.extend(events)
            self.metrics.extend(metric_rows)
            if metric_rows:
                self.metric_hosts.setdefault(agent, set()).update(r[1] for r in metric_rows)
            self.received += len(obs)
            self.agents[agent] = time.time()
            self._rate.append((time.time(), len(obs)))
        obs = events
        self._persist(events, agent, env)
        if self.on_ingest and events:
            try:
                self.on_ingest(events)
            except Exception:  # noqa: BLE001 - history is best effort
                pass
        if self.spool and obs:
            try:
                self._rotate_spool()
                with self.spool.open("a", encoding="utf-8") as f:
                    for o in obs:
                        f.write(json.dumps({"ts": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
                                            "env": o.environment, "message": o.message, "source": o.source, "agent": agent}, ensure_ascii=False) + "\n")
            except OSError:                                    # a full disk must not stop ingestion into the ring buffer
                pass
        return len(obs)

    SPOOL_MAX_BYTES = 64 * 1024 * 1024                         # events.jsonl is rotated at 64 MB; 3 generations are kept
    SPOOL_KEEP = 3

    def _rotate_spool(self) -> None:
        if not self.spool or not self.spool.exists() or self.spool.stat().st_size < self.spool_max_bytes:
            return
        for i in range(self.SPOOL_KEEP, 0, -1):
            src = self.spool.with_name(f"{self.spool.name}.{i - 1}") if i > 1 else self.spool
            dst = self.spool.with_name(f"{self.spool.name}.{i}")
            if src.exists():
                src.replace(dst)

    def snapshot(self, env: str | None = None, host: str | None = None, since=None) -> list[Observation]:
        """Events (optionally one env / host, optionally not older than `since`). Events arrive roughly in time order, so a
        `since` scan walks the buffer from the newest end and stops once a run of 2000 older events is seen instead of
        touching all 50k on every 2-second refresh."""
        from_db = False
        with self.lock:
            if since is None:
                obs = list(self.buf)
            else:
                s_cmp = since if since.tzinfo else since.replace(tzinfo=UTC)
                oldest = self.buf[0].timestamp if self.buf else None
                if oldest is not None and oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=UTC)
                from_db = bool(self.db) and (oldest is None or oldest > s_cmp)      # the ring no longer reaches back to `since`
                obs, stale = [], 0
                if not from_db:
                    for o in reversed(self.buf):
                        if o.timestamp >= since:
                            obs.append(o); stale = 0
                        else:
                            stale += 1
                            if stale > 2000:
                                break
                    obs.reverse()
        if from_db:
            try:
                obs = self._window_from_db(since)
            except Exception:  # noqa: BLE001 - a database hiccup degrades to the ring, never to an error
                with self.lock:
                    obs = [o for o in self.buf if o.timestamp >= since]
        if env:
            obs = [o for o in obs if (o.environment or "unknown") == env]
        if host:
            obs = [o for o in obs if host_match(o.host, host)]
        return obs

    def cached(self, name: str, *args):
        """Memoised query for the refreshing panels: the same (query, args) within the same 2-second tick and the same
        buffer version is computed once, however many widgets ask for it."""
        key = (name, args)
        version = (self.received, len(self.metrics), int(time.time() // 2))
        hit = self._memo.get(key)
        if hit and hit[0] == version:
            return hit[1]
        if len(self._memo) > 64:
            self._memo.clear()
        out = getattr(self, name)(*args)
        self._memo[key] = (version, out)
        return out

    def environments(self) -> dict[str, int]:
        return dict(Counter((o.environment or "unknown") for o in self.snapshot()).most_common())

    def hosts(self) -> dict[str, str]:
        """host -> environment for every host seen in events or metrics."""
        out: dict[str, str] = {}
        for o in self.snapshot():
            if o.host:
                out.setdefault(o.host, o.environment or "unknown")
        with self.lock:
            for r in self.metrics:
                out.setdefault(r[1], r[4] or "unknown")
        return dict(sorted(out.items()))

    def files(self, window_min: int = 15, env: str | None = None, host=None) -> list[dict]:
        """Which log files (per sender / host) the events came from, with totals, ERROR+ counts and the last error."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        out: dict[tuple, dict] = {}
        for o in self.snapshot(env, host, since=start):
            if o.attributes.get("discovery_app"):
                continue
            k = (o.attributes.get("agent", "-"), o.host or "-", o.source or "-")
            g = out.setdefault(k, {"agent": k[0], "host": k[1], "file": k[2], "total": 0, "errors": 0, "last": o.timestamp, "last_error": None, "last_msg": "", "services": set()})
            g["total"] += 1
            g["last"] = max(g["last"], o.timestamp)
            if o.service:
                g["services"].add(o.service)
            if SEV_RANK[o.severity] >= 3:
                g["errors"] += 1
                if g["last_error"] is None or o.timestamp >= g["last_error"]:
                    g["last_error"], g["last_msg"] = o.timestamp, o.message
        rows = [dict(g, services=sorted(g["services"])[:5]) for g in out.values()]
        return sorted(rows, key=lambda r: (-r["errors"], -r["total"]))

    def file_lines(self, agent: str, source: str, n: int = 400, errors_only: bool = False, host: str | None = None) -> list[Observation]:
        obs = [o for o in self.snapshot() if o.attributes.get("agent", "-") == agent and (o.source or "-") == source and (host is None or o.host == host)
               and (not errors_only or SEV_RANK[o.severity] >= 3)]
        return obs[-n:]

    def discoveries(self) -> dict[str, list[dict]]:
        """agent -> [{app, files}] from the discovery events agents send at start-up (latest per app)."""
        out: dict[str, dict[str, dict]] = {}
        for o in self.snapshot():
            app = o.attributes.get("discovery_app")
            if app:
                files = o.attributes.get("discovery_files") or []
                out.setdefault(o.attributes.get("agent", "-"), {})[app] = {"app": app, "files": files if isinstance(files, list) else [str(files)], "ts": o.timestamp}
        return {a: sorted(v.values(), key=lambda x: x["app"]) for a, v in out.items()}

    def tail(self, n: int = 50, env: str | None = None, host: str | None = None) -> list[Observation]:
        return self.snapshot(env, host)[-n:]

    def stats(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """Per-minute counts by severity for the last window, top services, totals (optionally one env / host)."""
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=window_min - 1)
        per_min: Counter = Counter()
        services: Counter = Counter()
        errors = 0
        obs = self.snapshot(env, host, since=start)
        for o in obs:
            if o.timestamp >= start:
                per_min[(o.timestamp.replace(second=0, microsecond=0), o.severity)] += 1
                if o.service:
                    services[o.service] += 1
                if SEV_RANK[o.severity] >= 3:
                    errors += 1
        rows = [{"minute": (start + timedelta(minutes=i)), "severity": sev, "events": per_min.get((start + timedelta(minutes=i), sev), 0)}
                for i in range(window_min) for sev in SEV_RANK]
        recent = sum(1 for o in obs if o.timestamp >= now - timedelta(minutes=1))
        if env or host:                                          # "total" is the whole scoped buffer, not only the window
            total = len(self.snapshot(env, host))
        else:
            with self.lock:
                total = len(self.buf)
        return {"rows": rows, "services": services.most_common(8), "total": total, "received": self.received,
                "errors": errors, "per_minute_now": recent, "agents": {k: round(time.time() - v) for k, v in self.agents.items()},
                "last": obs[-1].timestamp if obs else None}

    def to_dataset(self) -> tuple[str, bytes]:
        """Everything in the buffer as a JSONL dataset for the full pipeline."""
        lines = [json.dumps({"ts": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host, "env": o.environment,
                             "message": o.message, **{k: v for k, v in o.attributes.items() if k != "agent"}}, ensure_ascii=False, default=str)
                 for o in self.snapshot()]
        return "live_buffer.jsonl", ("\n".join(lines) + "\n").encode()

    def clear(self) -> None:
        with self.lock:
            self.buf.clear()
            self.metrics.clear()

    def purge(self, agent: str, hosts: tuple = ()) -> int:
        """Drop everything one sender shipped (simulation mode off): its events, its metrics rows (by host) and its presence."""
        with self.lock:
            keep = [o for o in self.buf if o.attributes.get("agent") != agent]
            n = len(self.buf) - len(keep)
            self.buf.clear(); self.buf.extend(keep)
            gone = set(hosts) | self.metric_hosts.pop(agent, set())
            if gone:
                mk = [r for r in self.metrics if r[1] not in gone]
                self.metrics.clear(); self.metrics.extend(mk)
            self.agents.pop(agent, None)
        return n

    # ---- infra metrics
    def metric_stats(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """Latest value per (metric, host), per-minute mean series, and threshold breaches (optionally one env / host)."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        with self.lock:
            rows = [r for r in self.metrics if r[0] >= start and (not env or (r[4] or "unknown") == env) and (not host or host_match(r[1], host))]
        latest: dict[tuple[str, str], float] = {}
        series: dict[tuple, list] = defaultdict(list)
        host_env = {r[1]: (r[4] or "unknown") for r in rows}
        for ts, host_, metric, value, _env in rows:
            host = host_
            latest[(metric, host)] = value
            series[(ts.replace(second=0, microsecond=0), host, metric)].append(value)
        per_min = [{"minute": k[0], "host": k[1], "metric": k[2], "env": host_env.get(k[1], "unknown"), "value": round(statistics.fmean(v), 1)}
                   for k, v in sorted(series.items())]
        thr = scenario.METRIC_THRESHOLDS
        breaches = [{"metric": m, "host": h, "value": v, "threshold": thr[m]} for (m, h), v in latest.items() if m in thr and v >= thr[m]]
        summary = {}
        for metric in METRICS:
            vals = [v for (m, _h), v in latest.items() if m == metric]
            summary[metric] = {"avg": round(statistics.fmean(vals), 1) if vals else None, "max": round(max(vals), 1) if vals else None,
                               "hosts": len(vals), "worst": max(((v, h) for (m, h), v in latest.items() if m == metric), default=(None, None))[1]}
        per_host = {h: {"env": e, **{m: latest.get((m, h)) for m in METRICS}} for h, e in sorted(host_env.items())}
        return {"latest": latest, "per_minute": per_min, "breaches": breaches, "summary": summary, "samples": len(rows), "per_host": per_host}

    # ---- service levels
    def slo(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """Availability = 1 - ERROR+/total, p95 latency from '<n>ms' in messages, error budget vs scenario.SLO / SLA."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        obs = self.snapshot(env, host, since=start)
        total = len(obs)
        errors = sum(1 for o in obs if SEV_RANK[o.severity] >= 3)
        avail = 1 - errors / total if total else None
        lat = [float(m[1]) for o in obs for m in [LATENCY_RE.search(o.message)] if m]
        lat.sort()
        p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) >= 20 else (lat[-1] if lat else None)
        slo, sla = scenario.SLO, scenario.SLA
        budget = None
        if avail is not None:
            allowed = 1 - slo["availability"]
            budget = max(0.0, 1 - (1 - avail) / allowed) if allowed > 0 else None
        return {"availability": avail, "p95_ms": p95, "error_budget": budget, "total": total, "errors": errors,
                "slo_ok": avail is not None and avail >= slo["availability"] and (p95 is None or p95 <= slo["p95_ms"]),
                "sla_ok": avail is None or avail >= sla["availability"], "slo": slo, "sla": sla}

    def slo_detail(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """What drives the service-level cards: error events by service / host / template, latency per service,
        per-minute availability, error-budget burn and the most recent ERROR+ events (same scope as slo())."""
        from .analysis import template_of
        now = datetime.now(UTC)
        start = now - timedelta(minutes=window_min)
        obs = [o for o in self.snapshot(env, host) if o.timestamp >= start]
        errs = [o for o in obs if SEV_RANK[o.severity] >= 3]
        by_service = Counter((o.service or "-") for o in errs).most_common()
        by_host = Counter((o.host or "-") for o in errs).most_common()
        tpl: dict[str, dict] = {}
        for o in errs:
            k = template_of(o.message)
            g = tpl.setdefault(k, {"template": k, "count": 0, "severity": o.severity, "first": o.timestamp, "last": o.timestamp, "services": set(), "hosts": set(), "sample": o.message})
            g["count"] += 1
            g["first"], g["last"] = min(g["first"], o.timestamp), max(g["last"], o.timestamp)
            if SEV_RANK[o.severity] > SEV_RANK[g["severity"]]: g["severity"] = o.severity
            if o.service: g["services"].add(o.service)
            if o.host: g["hosts"].add(o.host)
        templates = sorted((dict(g, services=sorted(g["services"]), hosts=sorted(g["hosts"])) for g in tpl.values()), key=lambda g: -g["count"])
        lat: dict[str, list[float]] = defaultdict(list)
        slow: list[dict] = []
        for o in obs:
            m = LATENCY_RE.search(o.message)
            if m:
                v = float(m[1])
                lat[o.service or "-"].append(v)
                slow.append({"ts": o.timestamp, "service": o.service or "-", "host": o.host or "-", "ms": v, "message": o.message})
        def q(v: list[float], p: float) -> float:
            v = sorted(v); return v[max(0, min(len(v) - 1, int(len(v) * p) - 1))]
        latency = sorted(({"service": k, "n": len(v), "p50": q(v, .5), "p95": q(v, .95), "max": max(v)} for k, v in lat.items()), key=lambda r: -r["p95"])
        slow.sort(key=lambda r: -r["ms"])
        start_min = now.replace(second=0, microsecond=0) - timedelta(minutes=window_min - 1)
        tot_m: Counter = Counter(); err_m: Counter = Counter()
        for o in obs:
            k = o.timestamp.replace(second=0, microsecond=0)
            tot_m[k] += 1
            if SEV_RANK[o.severity] >= 3: err_m[k] += 1
        target = scenario.SLO["availability"]
        allowed = 1 - target
        per_minute, cum_err, cum_tot = [], 0, 0
        for i in range(window_min):
            k = start_min + timedelta(minutes=i)
            n, e = tot_m.get(k, 0), err_m.get(k, 0)
            cum_err += e; cum_tot += n
            per_minute.append({"minute": k, "total": n, "errors": e, "availability": (1 - e / n) if n else None,
                               "budget_left": max(0.0, 1 - (cum_err / cum_tot) / allowed) if cum_tot and allowed > 0 else None})
        recent = [{"ts": o.timestamp, "severity": o.severity, "service": o.service or "-", "host": o.host or "-", "message": o.message} for o in errs[-40:]][::-1]
        return {"by_service": by_service, "by_host": by_host, "templates": templates[:10], "latency": latency, "slowest": slow[:10],
                "per_minute": per_minute, "recent": recent, "errors": len(errs), "total": len(obs),
                "allowed_errors": int(allowed * len(obs)), "breach_minutes": sum(1 for r in per_minute if r["availability"] is not None and r["availability"] < target)}

    def env_summary(self, window_min: int = 15) -> list[dict]:
        """Per-environment events, errors, availability and host count for the last window."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        obs = [o for o in self.snapshot() if o.timestamp >= start]
        hosts = self.hosts()
        out = []
        counts = Counter((o.environment or "unknown") for o in obs)
        for env in sorted(set(counts) | set(hosts.values()), key=lambda e: (ENV_ORDER.index(e) if e in ENV_ORDER else len(ENV_ORDER), e)):
            n = counts.get(env, 0)
            errs = sum(1 for o in obs if (o.environment or "unknown") == env and SEV_RANK[o.severity] >= 3)
            out.append({"environment": env, "events": n, "errors": errs, "availability": round(1 - errs / n, 4) if n else None,
                        "hosts": sum(1 for h, e in hosts.items() if e == env)})
        return out


def metric_of(o: Observation) -> list[tuple] | None:
    """Recognise metric samples: {"metric":"cpu","value":73} or {"cpu":73,"memory":55,"disk":80} style records."""
    a = o.attributes
    out: list[tuple] = []
    env = o.environment or ""
    name = str(a.get("metric") or a.get("name") or "").lower()
    if name in METRIC_KEYS and a.get("value") not in (None, ""):
        try:
            out.append((o.timestamp, o.host or o.service or "-", METRIC_KEYS[name], float(a["value"]), env))
        except (TypeError, ValueError):
            return None
        return out
    for k, v in a.items():
        if k.lower() in METRIC_KEYS:
            try:
                out.append((o.timestamp, o.host or o.service or "-", METRIC_KEYS[k.lower()], float(v), env))
            except (TypeError, ValueError):
                continue
    return out or None


MAX_BODY = 16 * 1024 * 1024                                    # one POST at most 16 MB (agents batch every second anyway)
AGENT_FILES = {"/agent.py": Path(__file__).resolve().parents[2] / "agent.py", "/agent/install.sh": Path(__file__).resolve().parents[2] / "scripts" / "agent-install.sh"}


def make_handler(store: LiveStore, api_key: str | None, registry=None):
    """Auth order: per-agent token from the registry (identity = the registered record) > legacy shared key > open when no key is set.
    GET /agent.py and /agent/install.sh serve the collector itself, so a server needs nothing but python3 and this port."""
    class H(BaseHTTPRequestHandler):
        def _token(self) -> str:
            auth = self.headers.get("Authorization", "")
            return self.headers.get("X-API-Key") or (auth[7:] if auth.startswith("Bearer ") else "")
        timeout = 30                                         # a stalled client cannot pin a handler thread forever

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            tok = self._token()
            if registry is not None and tok:
                self.agent_rec = registry.verify(tok)
                if self.agent_rec:
                    return True
                if tok.startswith("wo_"):                    # an agent token that is unknown or revoked: never fall back to the shared key
                    return False
            self.agent_rec = None
            if not api_key:                                  # open mode only until the first agent is enrolled: then every sender needs a token
                if registry is not None and registry.active_count() > 0:
                    return False
                return True
            return hmac.compare_digest(tok.encode(), api_key.encode())

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, {"status": "ok", "received": store.received, "buffered": len(store.buf), "agents": len(store.agents)})
            path = self.path.split("?", 1)[0]
            if path in AGENT_FILES and AGENT_FILES[path].exists():
                data = AGENT_FILES[path].read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers()
                return self.wfile.write(data)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.startswith("/enroll"):
                return self._enroll()
            if not self.path.startswith("/ingest"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "invalid or revoked token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if n <= 0:
                return self._send(400, {"error": "empty body"})
            if n > MAX_BODY:
                return self._send(413, {"error": f"body larger than {MAX_BODY // (1024 * 1024)} MB: send smaller batches"})
            data = self.rfile.read(n)
            if not data:
                return self._send(400, {"error": "empty body"})
            rec = getattr(self, "agent_rec", None)
            agent = (rec["name"] if rec else self.headers.get("X-Agent", self.client_address[0]))[:120]
            name = Path(self.headers.get("X-File-Name", "agent.log")).name[:200] or "agent.log"
            try:
                count = store.ingest(name, data, agent, env=(rec or {}).get("env", ""), site=(rec or {}).get("site", ""), agent_id=(rec or {}).get("id"))
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})
            if rec and registry is not None:
                try:
                    registry.touch(rec["id"], self.client_address[0], count)
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, {"accepted": count, "agent": agent, "identity": "token" if rec else ("key" if api_key else "open")})

        def _enroll(self):
            """A server presents the fleet enrolment key and gets its own token: {name, env, site, tags} in a JSON body."""
            if registry is None:
                return self._send(404, {"error": "no registry"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(max(n, 0), 64 * 1024)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad body"})
            name = str(body.get("name") or self.headers.get("X-Agent") or self.client_address[0])[:120]
            try:
                rec, token = registry.self_enroll(self._token(), name, str(body.get("env", ""))[:40], str(body.get("site", ""))[:80], str(body.get("tags", ""))[:200])
            except PermissionError:
                return self._send(401, {"error": "enrolment key is wrong or disabled"})
            except ValueError as e:
                return self._send(409, {"error": str(e)})
            self._send(200, {"token": token, "agent": rec["name"], "env": rec["env"], "site": rec["site"]})

    return H


def start_receiver(store: LiveStore, port: int = 8600, api_key: str | None = None, registry=None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, api_key, registry))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name=f"live-receiver-{port}").start()
    return server


# ---------------------------------------------------------------- built-in simulator (for demos when no agent is connected)
SIM_SERVICES = ["payment-api", "checkout-api", "auth-api", "search-api", "notification-service"]
SIM_HOSTS = ["prd-api-01", "prd-api-02", "prd-api-03", "worker-01", "db-01"]
SIM_GPU_HOSTS = ["ml-01", "ml-02"]
SIM_ENV = {"prd-api-01": "prod", "prd-api-02": "prod", "prd-api-03": "prod", "db-01": "prod", "worker-01": "staging", "ml-01": "dev", "ml-02": "qa"}


def simulate_batch(rng, n: int = 8, incident: bool = False) -> bytes:
    """A small batch of realistic JSONL events; `incident` injects an error burst."""
    now = datetime.now(UTC)
    out = []
    for _ in range(n):
        svc, host = rng.choice(SIM_SERVICES), rng.choice(SIM_HOSTS + SIM_GPU_HOSTS)
        if host in SIM_GPU_HOSTS:
            svc = rng.choice(("model-serving", "training-job"))
        if incident and rng.random() < 0.7:
            lvl, msg = "error", f"Connection timeout to db-01 (172.16.1.55:5432) after 5000ms request={rng.randint(100000, 999999)}"
        else:
            r = rng.random()
            lvl = "error" if r < 0.03 else "warn" if r < 0.10 else "info"
            msg = {"info": f"GET /api/v1/{rng.choice(['users', 'orders', 'cart'])}/{rng.randint(1, 9999)} 200 {rng.randint(8, 120)}ms",
                   "warn": f"cache miss ratio 0.{rng.randint(30, 45)} above 0.30",
                   "error": f"HTTP 500 upstream {rng.choice(SIM_SERVICES)} req={rng.randint(100000, 999999)}"}[lvl]
        out.append(json.dumps({"ts": now.isoformat(), "level": lvl, "service": svc, "host": host, "env": SIM_ENV.get(host, "prod"), "msg": msg}))
    return ("\n".join(out) + "\n").encode()


def simulate_metrics(rng, state: dict, incident: bool = False) -> bytes:
    """One CPU / memory / disk sample per host; random walk, CPU spikes during incidents, disk creeps up."""
    now = datetime.now(UTC).isoformat()
    out = []
    for host in SIM_HOSTS:
        st = state.setdefault(host, {"cpu": rng.uniform(25, 55), "memory": rng.uniform(45, 70), "disk": rng.uniform(40, 75)})
        st["cpu"] = min(99, max(3, st["cpu"] + rng.uniform(-6, 6) + (25 if incident and host == "db-01" else 0)))
        st["memory"] = min(99, max(10, st["memory"] + rng.uniform(-1.5, 1.8)))
        st["disk"] = min(99, st["disk"] + rng.uniform(0, 0.05))
        out.append(json.dumps({"ts": now, "host": host, "env": SIM_ENV.get(host, "prod"), "cpu": round(st["cpu"], 1), "memory": round(st["memory"], 1), "disk": round(st["disk"], 1)}))
    for host in SIM_GPU_HOSTS:
        st = state.setdefault(host, {"cpu": rng.uniform(15, 35), "gpu": rng.uniform(40, 80), "memory": rng.uniform(50, 75), "disk": rng.uniform(30, 60)})
        st["gpu"] = min(100, max(0, st["gpu"] + rng.uniform(-8, 8) + (15 if incident and host == "ml-01" else 0)))
        st["cpu"] = min(99, max(3, st["cpu"] + rng.uniform(-3, 3)))
        st["memory"] = min(99, max(10, st["memory"] + rng.uniform(-1, 1.2)))
        out.append(json.dumps({"ts": now, "host": host, "env": SIM_ENV.get(host, "dev"), "cpu": round(st["cpu"], 1), "gpu": round(st["gpu"], 1), "memory": round(st["memory"], 1), "disk": round(st["disk"], 1)}))
    return ("\n".join(out) + "\n").encode()


def start_simulator(store: LiveStore, interval: float = 1.0, agent: str = "simulator") -> threading.Event:
    """Feed the store every `interval` seconds until the returned Event is set. Bursts every ~2 minutes."""
    import random
    stop = threading.Event()
    rng = random.Random(42)
    mstate: dict = {}

    def run():
        tick = 0
        while not stop.is_set():
            tick += 1
            incident = 90 <= tick % 150 < 110
            store.ingest("sim.jsonl", simulate_batch(rng, rng.randint(4, 12), incident), agent)
            if tick % 5 == 1:
                store.ingest("metrics.jsonl", simulate_metrics(rng, mstate, incident), agent)
            stop.wait(interval)

    threading.Thread(target=run, daemon=True, name="live-simulator").start()
    return stop
