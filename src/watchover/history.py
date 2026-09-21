"""Service-level history: minute rollups of the live feed kept in the knowledge database so weekly and monthly reports
do not depend on the in-memory ring buffer. Written by a hook on LiveStore.ingest, flushed by a background thread.

Tables: slo_minute (minute × env × host × sender: totals, errors, latency histogram) and slo_hour_service (hour × env ×
service: totals, errors). Retention is pruned on flush. Figures for a period come back in the same shape report.py uses."""
from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .models import SEV_RANK
from . import scenario

UTC = timezone.utc
LAT_EDGES = (50, 100, 200, 300, 500, 1000, 2000, 5000)          # ms upper bounds; bucket 8 = above 5000
LAT_COLS = [f"l{i}" for i in range(len(LAT_EDGES) + 1)]


def _bucket(ms: float) -> int:
    for i, e in enumerate(LAT_EDGES):
        if ms <= e:
            return i
    return len(LAT_EDGES)


class History:
    def __init__(self, kb, retention_days: int = 400):
        self.kb, self.retention_days = kb, retention_days
        self.pending_min: dict = defaultdict(lambda: [0, 0] + [0] * len(LAT_COLS))
        self.pending_svc: dict = defaultdict(lambda: [0, 0])
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.flushed = 0
        self.last_flush = ""
        lat = ", ".join(f"{c} INTEGER DEFAULT 0" for c in LAT_COLS)
        kb._exec(f"""CREATE TABLE IF NOT EXISTS slo_minute (minute TEXT NOT NULL, env TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
            sender TEXT NOT NULL DEFAULT '', total INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, {lat}, PRIMARY KEY (minute, env, host, sender))""")
        kb._exec("""CREATE TABLE IF NOT EXISTS slo_hour_service (hour TEXT NOT NULL, env TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            total INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, PRIMARY KEY (hour, env, service))""")
        kb._exec("CREATE INDEX IF NOT EXISTS slo_minute_t ON slo_minute(minute)")

    # ---- write side
    def add(self, events: list) -> None:
        """LiveStore hook: aggregate freshly ingested observations (never blocks on the database)."""
        from .live import LATENCY_RE
        with self.lock:
            for o in events:
                err = 1 if SEV_RANK.get(o.severity, 0) >= 3 else 0
                minute = o.timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")
                row = self.pending_min[(minute, o.environment or "unknown", o.host or "-", o.attributes.get("agent", "-"))]
                row[0] += 1; row[1] += err
                m = LATENCY_RE.search(o.message or "")
                if m:
                    row[2 + _bucket(float(m[1]))] += 1
                srow = self.pending_svc[(minute[:13], o.environment or "unknown", o.service or "-")]
                srow[0] += 1; srow[1] += err

    def flush(self) -> int:
        with self.lock:
            pm, ps = dict(self.pending_min), dict(self.pending_svc)
            self.pending_min.clear(); self.pending_svc.clear()
        if not pm and not ps:
            return 0
        lat_names = ", ".join(LAT_COLS)
        lat_upd = ", ".join(f"{c}=slo_minute.{c}+excluded.{c}" for c in LAT_COLS)
        for (minute, env, host, sender), v in pm.items():
            self.kb._exec(f"INSERT INTO slo_minute (minute, env, host, sender, total, errors, {lat_names}) VALUES ({', '.join('?' * (6 + len(LAT_COLS)))}) "
                          f"ON CONFLICT(minute, env, host, sender) DO UPDATE SET total=slo_minute.total+excluded.total, errors=slo_minute.errors+excluded.errors, {lat_upd}",
                          (minute, env, host, sender, *v))
        for (hour, env, service), v in ps.items():
            self.kb._exec("INSERT INTO slo_hour_service (hour, env, service, total, errors) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(hour, env, service) DO UPDATE SET total=slo_hour_service.total+excluded.total, errors=slo_hour_service.errors+excluded.errors", (hour, env, service, *v))
        self.flushed += len(pm)
        self.last_flush = datetime.now(UTC).isoformat(timespec="seconds")
        return len(pm)

    def prune(self) -> None:
        cut = (datetime.now(UTC) - timedelta(days=self.retention_days)).strftime("%Y-%m-%dT%H:%M")
        self.kb._exec("DELETE FROM slo_minute WHERE minute < ?", (cut,))
        self.kb._exec("DELETE FROM slo_hour_service WHERE hour < ?", (cut[:13],))

    def start(self, every: float = 30.0) -> "History":
        def loop():
            n = 0
            while not self.stop.is_set():
                self.stop.wait(every)
                try:
                    self.flush()
                    n += 1
                    if n % 120 == 0:
                        self.prune()
                except Exception:  # noqa: BLE001 - a database hiccup must not kill the flusher
                    pass
        threading.Thread(target=loop, daemon=True, name="slo-history").start()
        return self

    # ---- read side
    def coverage(self) -> dict:
        r = self.kb._exec("SELECT MIN(minute) AS first, MAX(minute) AS last, COUNT(*) AS rows_ FROM slo_minute")
        return dict(r[0]) if r else {"first": None, "last": None, "rows_": 0}

    def figures(self, start: datetime, end: datetime, env: str | None = None, host=None, bucket: str = "hour") -> dict:
        """Same shape as report.figures_live(): availability, p95, budget, series (per bucket), breakdowns."""
        hosts = [host] if isinstance(host, str) else (list(host) if host else None)
        cond, params = ["minute >= ?", "minute < ?"], [start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")]
        if env:
            cond.append("env = ?"); params.append(env)
        if hosts:
            cond.append(f"host IN ({', '.join('?' * len(hosts))})"); params += hosts
        rows = self.kb._exec(f"SELECT * FROM slo_minute WHERE {' AND '.join(cond)}", tuple(params))
        slo, sla = scenario.SLO, scenario.SLA
        total = sum(r["total"] for r in rows); errors = sum(r["errors"] for r in rows)
        hist = [sum(r[c] for r in rows) for c in LAT_COLS]
        n_lat = sum(hist)
        p95 = None
        if n_lat:
            acc = 0
            for i, c in enumerate(hist):
                acc += c
                if acc >= 0.95 * n_lat:
                    p95 = float(LAT_EDGES[i] if i < len(LAT_EDGES) else LAT_EDGES[-1] * 2); break
        avail = 1 - errors / total if total else None
        allowed = 1 - slo["availability"]
        budget = max(0.0, 1 - (1 - avail) / allowed) if avail is not None and allowed > 0 else None
        # series per bucket
        key_len = {"minute": 16, "hour": 13, "day": 10}[bucket]
        step = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}[bucket]
        per: dict = defaultdict(lambda: [0, 0])
        for r in rows:
            k = r["minute"][:key_len]; per[k][0] += r["total"]; per[k][1] += r["errors"]
        series, cum_t, cum_e = [], 0, 0
        cur = start.replace(second=0, microsecond=0)
        if bucket == "hour":
            cur = cur.replace(minute=0)
        elif bucket == "day":
            cur = cur.replace(hour=0, minute=0)
        while cur < end:
            k = cur.strftime("%Y-%m-%dT%H:%M")[:key_len]
            tt, ee = per.get(k, [0, 0])
            cum_t += tt; cum_e += ee
            series.append({"label": cur.strftime("%d.%m" if bucket == "day" else "%d.%m %H:%M" if bucket == "hour" else "%H:%M"), "when": cur, "total": tt, "errors": ee,
                           "availability": (1 - ee / tt) if tt else None, "budget_left": max(0.0, 1 - (cum_e / cum_t) / allowed) if cum_t and allowed > 0 else None})
            cur += step
        by_host = Counter(); by_sender = Counter()
        for r in rows:
            by_host[r["host"]] += r["errors"]; by_sender[r["sender"]] += r["errors"]
        scond, sparams = ["hour >= ?", "hour <= ?"], [params[0][:13], params[1][:13]]   # inclusive: the end hour is still running
        if env:
            scond.append("env = ?"); sparams.append(env)
        srows = self.kb._exec(f"SELECT service, SUM(total) AS t, SUM(errors) AS e FROM slo_hour_service WHERE {' AND '.join(scond)} GROUP BY service ORDER BY e DESC", tuple(sparams))
        by_service = [(r["service"], int(r["e"])) for r in srows if r["e"]] if not hosts else []   # service split is per env, not per host
        return {"availability": avail, "p95_ms": p95, "error_budget": budget, "total": total, "errors": errors, "allowed_errors": int(allowed * total),
                "breach_buckets": sum(1 for s in series if s["availability"] is not None and s["availability"] < slo["availability"]),
                "series": series, "bucket": bucket, "by_service": by_service, "by_host": [(h, n) for h, n in by_host.most_common(10) if n],
                "by_sender": [(a, n) for a, n in by_sender.most_common(10) if n], "latency": [], "templates": [], "slo": slo, "sla": sla,
                "slo_ok": avail is not None and avail >= slo["availability"] and (p95 is None or p95 <= slo["p95_ms"]),
                "sla_ok": avail is None or avail >= sla["availability"], "source": "history", "lat_hist": dict(zip(LAT_COLS, hist))}
