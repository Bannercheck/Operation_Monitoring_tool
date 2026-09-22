#!/usr/bin/env python3
"""Load test: N virtual agents push a realistic ERP-heavy log mix at a target aggregate rate and the script reports what the
receiver, the database window and the anomaly scan actually did.

  python scripts/loadtest.py --url http://localhost:8600 --api http://localhost:8501 --rate 2000 --agents 100 --duration 60 \\
      --user admin@watchover.local --password '…' [--key LIVE_KEY]

Lines come from the corpus (SAP, Oracle EBS, Dynamics AX / 365 / NAV, syslog, JSON) with fresh timestamps and ids, so the
parsers work as hard as they would on real traffic. Nothing is exploited or flooded beyond the rate you ask for; run it
against your own Watchover only. Writes loadtest-report.json.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
# corpus file -> (X-File-Name to send, share of the mix). ERP-heavy: 60 % ERP, 40 % infrastructure.
MIX = [("sap_ccms.log", "sap_ccms.log", 12), ("sap_dev_w0.log", "dev_w0.log", 12), ("oracle_ebs_concurrent.log", "ebs_concurrent.log", 12),
       ("dynamics_ax_aos.log", "ax_aos.log", 8), ("dynamics_365_batch.log", "d365_batch.log", 8), ("dynamics_nav_bc.log", "nav_bc.log", 8),
       ("syslog_rfc3164.log", "syslog.log", 15), ("app_json.jsonl", "app.jsonl", 15), ("nginx_access.log", "access.log", 10)]
TS_RES = [re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), re.compile(r"\b[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2}\b"),
          re.compile(r"\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}")]


def load_templates() -> list[tuple[str, list[str], int]]:
    out = []
    for fname, send_as, weight in MIX:
        p = ROOT / "samples" / "corpus" / fname
        if not p.exists():
            alt = sorted((ROOT / "samples" / "corpus").glob(fname.split("_")[0] + "*"))
            if not alt:
                continue
            p = alt[0]
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        if lines:
            out.append((send_as, lines, weight))
    if not out:
        sys.exit("no corpus files found under samples/corpus")
    return out


def freshen(line: str, now: datetime, rng: random.Random) -> str:
    iso = now.strftime("%Y-%m-%dT%H:%M:%S") + f".{rng.randint(0, 999):03d}Z"
    bsd = now.strftime("%b %d %H:%M:%S")
    clf = now.strftime("%d/%b/%Y:%H:%M:%S +0000")
    line = TS_RES[0].sub(iso, line, count=1); line = TS_RES[1].sub(bsd, line, count=1); line = TS_RES[2].sub(clf, line, count=1)
    line = re.sub(r"\b(\d{5,6})\b", lambda m: str(rng.randint(10000, 999999)), line, count=2)    # job / session / order ids vary (never a 4-digit year)
    if rng.random() < 0.02:                                                                        # a little real trouble in the mix
        line = line.replace("INFO", "ERROR").replace("started", "failed: connection timeout to db-01")
    return line


class Agent(threading.Thread):
    def __init__(self, idx: int, url: str, key: str | None, templates, per_batch: int, interval: float, until: float, stats: dict, lock: threading.Lock):
        super().__init__(daemon=True)
        self.idx, self.url, self.key, self.templates, self.per_batch, self.interval, self.until, self.stats, self.lock = idx, url, key, templates, per_batch, interval, until, stats, lock
        self.rng = random.Random(1000 + idx)
        self.name_ = f"load-{idx:03d}"
        weights = [w for _, _, w in templates]
        self.pick = lambda: self.rng.choices(templates, weights=weights)[0]

    def run(self) -> None:
        next_at = time.time() + self.rng.random() * self.interval
        while time.time() < self.until:
            now = time.time()
            if now < next_at:
                time.sleep(min(next_at - now, 0.05)); continue
            next_at += self.interval
            send_as, lines, _ = self.pick()
            stamp = datetime.now(UTC)
            body = "\n".join(freshen(self.rng.choice(lines), stamp, self.rng) for _ in range(self.per_batch)).encode()
            hdr = {"Content-Type": "text/plain", "X-Agent": self.name_, "X-File-Name": send_as}
            if self.key:
                hdr["X-API-Key"] = self.key
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url.rstrip("/") + "/ingest", data=body, headers=hdr, method="POST"), timeout=30) as r:
                    acc = json.loads(r.read() or b"{}").get("accepted", 0); ok = True
            except Exception as e:  # noqa: BLE001
                acc, ok = 0, False; err = f"{type(e).__name__}: {str(e)[:80]}"
            ms = (time.perf_counter() - t0) * 1000
            with self.lock:
                self.stats["sent"] += self.per_batch; self.stats["accepted"] += acc; self.stats["batches"] += 1; self.stats["lat"].append(ms)
                if not ok:
                    self.stats["errors"] += 1; self.stats["last_error"] = err


def api(base: str, path: str, tok: str | None = None, data=None, method="GET"):
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(data).encode() if data is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8600"); ap.add_argument("--api", default="http://localhost:8501")
    ap.add_argument("--rate", type=int, default=2000, help="target events/s across all agents"); ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--duration", type=int, default=60); ap.add_argument("--interval", type=float, default=2.0, help="seconds between two batches of one agent")
    ap.add_argument("--key", default=None); ap.add_argument("--user", default=None); ap.add_argument("--password", default=None)
    ap.add_argument("--out", default="loadtest-report.json")
    a = ap.parse_args(argv)
    templates = load_templates()
    per_batch = max(1, round(a.rate * a.interval / a.agents))
    print(f"{a.agents} agents · {per_batch} lines every {a.interval}s each -> target {a.agents * per_batch / a.interval:.0f} events/s for {a.duration}s · mix: {', '.join(t[0] for t in templates)}", flush=True)
    tok = None
    if a.user and a.password:
        try:
            tok = api(a.api, "/api/auth/login", data={"email": a.user, "password": a.password}, method="POST")["token"]
        except Exception as e:  # noqa: BLE001
            print(f"api login failed ({e}); capacity polling off", flush=True)
    stats = {"sent": 0, "accepted": 0, "batches": 0, "errors": 0, "lat": [], "last_error": ""}
    lock = threading.Lock(); until = time.time() + a.duration
    agents = [Agent(i, a.url, a.key, templates, per_batch, a.interval, until, stats, lock) for i in range(a.agents)]
    t_start = time.time()
    for ag in agents:
        ag.start()
    samples = []
    while time.time() < until:
        time.sleep(10)
        with lock:
            snap = dict(stats); snap["lat"] = None
        elapsed = time.time() - t_start
        line = f"t={elapsed:5.0f}s sent {snap['sent']:>8} ({snap['sent'] / elapsed:6.0f}/s) accepted {snap['accepted']:>8} errors {snap['errors']}"
        if tok:
            try:
                st = api(a.api, "/api/system/status", tok)
                cap = st.get("capacity", {}); an = st.get("anomalies", {})
                samples.append({"t": round(elapsed), "capacity": cap, "anomaly_runs": an.get("runs"), "last_scan_ms": an.get("last_scan_ms")})
                line += (f" | server {cap.get('events_per_s')}/s ring {cap.get('ring', {}).get('fill_pct')}% span {cap.get('ring', {}).get('span_s')}s "
                         f"ingest avg {cap.get('ingest', {}).get('avg_ms')}ms p95 {cap.get('ingest', {}).get('p95_ms')}ms db {cap.get('db', {}).get('events')} ev "
                         f"scan {an.get('last_scan_ms')}ms x{an.get('runs')} warn {[w['code'] for w in cap.get('warnings', [])]}")
            except Exception as e:  # noqa: BLE001
                line += f" | status: {type(e).__name__}"
        print(line, flush=True)
    for ag in agents:
        ag.join(timeout=35)
    elapsed = time.time() - t_start
    lat = sorted(stats["lat"])
    rep = {"target_rate": a.rate, "agents": a.agents, "per_batch": per_batch, "duration_s": round(elapsed, 1), "sent": stats["sent"], "accepted": stats["accepted"],
           "achieved_rate": round(stats["sent"] / elapsed, 1), "accepted_rate": round(stats["accepted"] / elapsed, 1), "batches": stats["batches"], "errors": stats["errors"], "last_error": stats["last_error"],
           "post_latency_ms": {"p50": round(statistics.median(lat), 1) if lat else 0, "p95": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) >= 20 else 0, "max": round(lat[-1], 1) if lat else 0},
           "samples": samples}
    if tok:
        try:
            t0 = time.perf_counter(); found = api(a.api, "/api/anomalies/scan", tok, data={}, method="POST"); scan_ms = (time.perf_counter() - t0) * 1000
            rep["final_scan"] = {"ms": round(scan_ms), "found": len(found) if isinstance(found, list) else found}
            rep["final_capacity"] = api(a.api, "/api/system/status", tok).get("capacity", {})
        except Exception as e:  # noqa: BLE001
            rep["final_scan"] = {"error": str(e)[:200]}
    Path(a.out).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\nRESULT", json.dumps({k: v for k, v in rep.items() if k not in ("samples", "final_capacity")}, ensure_ascii=False), flush=True)
    fc = rep.get("final_capacity", {})
    if fc:
        print("FINAL CAPACITY", json.dumps({"events_per_s_5m": fc.get("events_per_s_5m"), "ring": fc.get("ring"), "db": fc.get("db"), "ingest": fc.get("ingest"), "last_db_window_ms": fc.get("last_db_window_ms"), "warnings": fc.get("warnings")}, ensure_ascii=False))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
