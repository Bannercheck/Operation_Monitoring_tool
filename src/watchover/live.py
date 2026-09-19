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
        self.buf: deque[Observation] = deque(maxlen=maxlen)
        self.metrics: deque[tuple] = deque(maxlen=maxlen)     # (ts, host, metric, value, env)
        self.lock = threading.Lock()
        self.spool = Path(spool) if spool else None
        self.received = 0
        self.agents: dict[str, float] = {}      # agent name -> last seen epoch
        self.on_ingest = None                    # optional hook(events): service-level history rollups
        self.enricher = None                     # optional hook(observation) -> bool: inventory match (IP → hostname, dc, criticality)
        self.matched = 0                         # events matched to the inventory
        self.started = time.time()
        if self.spool:
            self.spool.parent.mkdir(parents=True, exist_ok=True)

    def ingest(self, name: str, data: bytes, agent: str = "unknown", env: str = "", site: str = "", agent_id: int | None = None) -> int:
        obs, _ = ingest_bytes(name, data)
        now = datetime.now(UTC)
        metric_rows: list[tuple] = []
        events: list[Observation] = []
        for o in obs:
            if o.attributes.pop("_no_ts", False) or o.timestamp.year < 2000:
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
            self.received += len(obs)
            self.agents[agent] = time.time()
        obs = events
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
        if not self.spool or not self.spool.exists() or self.spool.stat().st_size < self.SPOOL_MAX_BYTES:
            return
        for i in range(self.SPOOL_KEEP, 0, -1):
            src = self.spool.with_name(f"{self.spool.name}.{i - 1}") if i > 1 else self.spool
            dst = self.spool.with_name(f"{self.spool.name}.{i}")
            if src.exists():
                src.replace(dst)

    def snapshot(self, env: str | None = None, host: str | None = None) -> list[Observation]:
        with self.lock:
            obs = list(self.buf)
        if env:
            obs = [o for o in obs if (o.environment or "unknown") == env]
        if host:
            obs = [o for o in obs if host_match(o.host, host)]
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
