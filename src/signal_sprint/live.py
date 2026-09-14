"""Live ingest: agents POST events to a small HTTP receiver; the dashboard charts them as they arrive.

    store = LiveStore(spool="data/live/events.jsonl")
    start_receiver(store, port=8600, api_key="secret")     # daemon thread inside the Streamlit process
    POST /ingest  (X-API-Key or Authorization: Bearer)   body: JSON array | JSONL | CSV | syslog | any text
    GET  /health

Any format the pipeline parses is accepted, so the agent can ship raw log lines. Standard library only.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .models import SEV_RANK, Observation
from .pipeline import ingest_bytes

UTC = timezone.utc


class LiveStore:
    """Thread-safe ring buffer of recent observations + append-only JSONL spool on disk."""

    def __init__(self, spool: str | Path | None = None, maxlen: int = 50_000):
        self.buf: deque[Observation] = deque(maxlen=maxlen)
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
        for o in obs:
            if o.attributes.pop("_no_ts", False) or o.timestamp.year < 2000:
                o.timestamp = now
            o.attributes["agent"] = agent
        with self.lock:
            self.buf.extend(obs)
            self.received += len(obs)
            self.agents[agent] = time.time()
        if self.spool and obs:
            with self.spool.open("a", encoding="utf-8") as f:
                for o in obs:
                    f.write(json.dumps({"ts": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
                                        "message": o.message, "source": o.source, "agent": agent}, ensure_ascii=False) + "\n")
        return len(obs)

    def snapshot(self) -> list[Observation]:
        with self.lock:
            return list(self.buf)

    def tail(self, n: int = 50) -> list[Observation]:
        with self.lock:
            return list(self.buf)[-n:]

    def stats(self, window_min: int = 15) -> dict:
        """Per-minute counts by severity for the last window, top services, totals."""
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=window_min - 1)
        per_min: Counter = Counter()
        services: Counter = Counter()
        errors = 0
        obs = self.snapshot()
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
        lines = [json.dumps({"ts": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
                             "message": o.message, **{k: v for k, v in o.attributes.items() if k != "agent"}}, ensure_ascii=False, default=str)
                 for o in self.snapshot()]
        return "live_buffer.jsonl", ("\n".join(lines) + "\n").encode()

    def clear(self) -> None:
        with self.lock:
            self.buf.clear()


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


def simulate_batch(rng, n: int = 8, incident: bool = False) -> bytes:
    """A small batch of realistic JSONL events; `incident` injects an error burst."""
    now = datetime.now(UTC)
    out = []
    for _ in range(n):
        svc, host = rng.choice(SIM_SERVICES), rng.choice(SIM_HOSTS)
        if incident and rng.random() < 0.7:
            lvl, msg = "error", f"Connection timeout to db-01 (172.16.1.55:5432) after 5000ms request={rng.randint(100000, 999999)}"
        else:
            r = rng.random()
            lvl = "error" if r < 0.03 else "warn" if r < 0.10 else "info"
            msg = {"info": f"GET /api/v1/{rng.choice(['users', 'orders', 'cart'])}/{rng.randint(1, 9999)} 200 {rng.randint(8, 120)}ms",
                   "warn": f"cache miss ratio 0.{rng.randint(30, 45)} above 0.30",
                   "error": f"HTTP 500 upstream {rng.choice(SIM_SERVICES)} req={rng.randint(100000, 999999)}"}[lvl]
        out.append(json.dumps({"ts": now.isoformat(), "level": lvl, "service": svc, "host": host, "msg": msg}))
    return ("\n".join(out) + "\n").encode()


def start_simulator(store: LiveStore, interval: float = 1.0, agent: str = "simulator") -> threading.Event:
    """Feed the store every `interval` seconds until the returned Event is set. Bursts every ~2 minutes."""
    import random
    stop = threading.Event()
    rng = random.Random(42)

    def run():
        tick = 0
        while not stop.is_set():
            tick += 1
            incident = 90 <= tick % 150 < 110
            store.ingest("sim.jsonl", simulate_batch(rng, rng.randint(4, 12), incident), agent)
            stop.wait(interval)

    threading.Thread(target=run, daemon=True, name="live-simulator").start()
    return stop
