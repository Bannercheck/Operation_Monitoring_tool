"""Live ingest: agents POST events to a small HTTP receiver; the dashboard charts them as they arrive.

    store = LiveStore(spool="data/live/events.jsonl")
    start_receiver(store, port=8600, api_key="secret")     # daemon thread inside the Streamlit process
    POST /ingest  (X-API-Key or Authorization: Bearer)   body: JSON array | JSONL | CSV | syslog | any text
    GET  /health

Any format the pipeline parses is accepted, so the agent can ship raw log lines. Standard library only.
"""

from __future__ import annotations

import json
import re
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
METRIC_KEYS = {"cpu": "cpu", "cpu_percent": "cpu", "cpu_pct": "cpu", "memory": "memory", "mem": "memory", "mem_percent": "memory",
               "memory_percent": "memory", "disk": "disk", "disk_percent": "disk", "disk_pct": "disk",
               "gpu": "gpu", "gpu_percent": "gpu", "gpu_util": "gpu", "gpu_utilization": "gpu"}
METRICS = ("cpu", "gpu", "memory", "disk")
LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s?ms\b")


class LiveStore:
    """Thread-safe ring buffer of recent observations + append-only JSONL spool on disk."""

    def __init__(self, spool: str | Path | None = None, maxlen: int = 50_000):
        self.buf: deque[Observation] = deque(maxlen=maxlen)
        self.metrics: deque[tuple] = deque(maxlen=maxlen)     # (ts, host, metric, value, env)
        self.lock = threading.Lock()
        self.spool = Path(spool) if spool else None
        self.received = 0
        self.agents: dict[str, float] = {}      # agent name -> last seen epoch
        self.started = time.time()
        if self.spool:
            self.spool.parent.mkdir(parents=True, exist_ok=True)

    def ingest(self, name: str, data: bytes, agent: str = "unknown") -> int:
        obs, _ = ingest_bytes(name, data)
        now = datetime.now(UTC)
        metric_rows: list[tuple] = []
        events: list[Observation] = []
        for o in obs:
            if o.attributes.pop("_no_ts", False) or o.timestamp.year < 2000:
                o.timestamp = now
            o.attributes["agent"] = agent
            m = metric_of(o)
            if m:
                metric_rows.extend(m)
            else:
                events.append(o)
        with self.lock:
            self.buf.extend(events)
            self.metrics.extend(metric_rows)
            self.received += len(obs)
            self.agents[agent] = time.time()
        obs = events
        if self.spool and obs:
            with self.spool.open("a", encoding="utf-8") as f:
                for o in obs:
                    f.write(json.dumps({"ts": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
                                        "env": o.environment, "message": o.message, "source": o.source, "agent": agent}, ensure_ascii=False) + "\n")
        return len(obs)

    def snapshot(self, env: str | None = None, host: str | None = None) -> list[Observation]:
        with self.lock:
            obs = list(self.buf)
        if env:
            obs = [o for o in obs if (o.environment or "unknown") == env]
        if host:
            obs = [o for o in obs if o.host == host]
        return obs

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

    def tail(self, n: int = 50, env: str | None = None, host: str | None = None) -> list[Observation]:
        return self.snapshot(env, host)[-n:]

    def stats(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """Per-minute counts by severity for the last window, top services, totals (optionally one env / host)."""
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=window_min - 1)
        per_min: Counter = Counter()
        services: Counter = Counter()
        errors = 0
        obs = self.snapshot(env, host)
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
        return {"rows": rows, "services": services.most_common(8), "total": len(obs), "received": self.received,
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

    # ---- infra metrics
    def metric_stats(self, window_min: int = 15, env: str | None = None, host: str | None = None) -> dict:
        """Latest value per (metric, host), per-minute mean series, and threshold breaches (optionally one env / host)."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        with self.lock:
            rows = [r for r in self.metrics if r[0] >= start and (not env or (r[4] or "unknown") == env) and (not host or r[1] == host)]
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
        obs = [o for o in self.snapshot(env, host) if o.timestamp >= start]
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

    def env_summary(self, window_min: int = 15) -> list[dict]:
        """Per-environment events, errors, availability and host count for the last window."""
        start = datetime.now(UTC) - timedelta(minutes=window_min)
        obs = [o for o in self.snapshot() if o.timestamp >= start]
        hosts = self.hosts()
        out = []
        for env, n in Counter((o.environment or "unknown") for o in obs).most_common():
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


def make_handler(store: LiveStore, api_key: str | None):
    class H(BaseHTTPRequestHandler):
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
            if not api_key:
                return True
            auth = self.headers.get("Authorization", "")
            return self.headers.get("X-API-Key") == api_key or auth == f"Bearer {api_key}"

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, {"status": "ok", "received": store.received, "buffered": len(store.buf)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/ingest"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "invalid api key"})
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            if not data:
                return self._send(400, {"error": "empty body"})
            agent = self.headers.get("X-Agent", self.client_address[0])
            name = self.headers.get("X-File-Name", "agent.log")
            try:
                count = store.ingest(name, data, agent)
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})
            self._send(200, {"accepted": count})

    return H


def start_receiver(store: LiveStore, port: int = 8600, api_key: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, api_key))
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
