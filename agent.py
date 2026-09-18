"""Watchover agent: ships log lines to the dashboard's live receiver.

    python agent.py --url http://localhost:8600/ingest --key SECRET --tail /var/log/app/app.log
    python agent.py --url http://localhost:8600/ingest --key SECRET --simulate        # synthetic traffic
    python agent.py --url http://<dashboard>:8600/ingest --key SECRET --file export.csv  # one-shot upload

Any format works (JSON / JSONL / CSV / syslog / key=value / text); the receiver runs the same parsers as the dashboard.
Standard library only, so it runs on any host with Python 3.9+.
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("src")))
from watchover.live import simulate_batch  # noqa: E402


def host_metrics() -> dict:
    """CPU / memory / disk percent using only the standard library (Linux / macOS; falls back gracefully)."""
    import json as _json
    import os
    import shutil
    out = {"ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), "host": socket.gethostname()}
    try:
        load1 = os.getloadavg()[0]
        out["cpu"] = round(min(100.0, 100.0 * load1 / max(os.cpu_count() or 1, 1)), 1)
    except (OSError, AttributeError):
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, v = ln.split(":", 1)
                mem[k] = int(v.strip().split()[0])
        out["memory"] = round(100.0 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
    except (OSError, KeyError, ValueError):
        pass
    try:
        du = shutil.disk_usage("/")
        out["disk"] = round(100.0 * du.used / du.total, 1)
    except OSError:
        pass
    try:  # NVIDIA GPUs, if nvidia-smi exists
        import subprocess
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        vals = [float(x) for x in r.stdout.split() if x.strip().replace(".", "").isdigit()]
        if vals:
            out["gpu"] = round(max(vals), 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


def post(url: str, key: str | None, data: bytes, name: str, agent: str) -> int:
    headers = {"Content-Type": "application/octet-stream", "X-Agent": agent, "X-File-Name": name}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def tail(path: Path, url: str, key: str, agent: str, batch_secs: float = 1.0) -> None:
    print(f"tailing {path} -> {url}")
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # start at the end, like tail -f
        buf: list[str] = []
        last = time.time()
        while True:
            line = f.readline()
            if line:
                buf.append(line)
            else:
                time.sleep(0.2)
            if buf and time.time() - last >= batch_secs:
                try:
                    post(url, key, "".join(buf).encode(), path.name, agent)
                except Exception as e:  # noqa: BLE001
                    print("send failed:", e, file=sys.stderr)
                buf, last = [], time.time()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8600/ingest")
    ap.add_argument("--key", default=None, help="API key configured in Connection Settings")
    ap.add_argument("--agent", default=socket.gethostname())
    ap.add_argument("--env", default=os.environ.get("AGENT_ENV", ""), help="environment tag for this host (prod / test / dev / staging)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tail", help="follow a log file")
    g.add_argument("--file", help="send a file once")
    g.add_argument("--simulate", action="store_true", help="generate synthetic traffic")
    g.add_argument("--metrics", action="store_true", help="ship this host's CPU / memory / disk every --interval seconds")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)
    if args.file:
        p = Path(args.file)
        print("status", post(args.url, args.key, p.read_bytes(), p.name, args.agent))
        return 0
    if args.tail:
        tail(Path(args.tail), args.url, args.key, args.agent, args.interval)
        return 0
    if args.metrics:
        import json as _json
        print(f"shipping host metrics -> {args.url} (Ctrl+C to stop)")
        while True:
            try:
                m = host_metrics()
                if args.env:
                    m["env"] = args.env
                post(args.url, args.key, (_json.dumps(m) + "\n").encode(), "metrics.jsonl", args.agent)
            except Exception as e:  # noqa: BLE001
                print("send failed:", e, file=sys.stderr)
            time.sleep(max(args.interval, 1.0))
    rng = random.Random()
    tick = 0
    print(f"simulating -> {args.url} (Ctrl+C to stop)")
    while True:
        tick += 1
        try:
            post(args.url, args.key, simulate_batch(rng, rng.randint(4, 12), 90 <= tick % 150 < 110), "sim.jsonl", args.agent)
        except Exception as e:  # noqa: BLE001
            print("send failed:", e, file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
