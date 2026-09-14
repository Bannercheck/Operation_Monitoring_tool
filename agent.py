"""Signal Sprint agent: ships log lines to the dashboard's live receiver.

    python agent.py --url http://localhost:8600/ingest --key SECRET --tail /var/log/app/app.log
    python agent.py --url http://localhost:8600/ingest --key SECRET --simulate        # synthetic traffic
    python agent.py --url http://<dashboard>:8600/ingest --key SECRET --file export.csv  # one-shot upload

Any format works (JSON / JSONL / CSV / syslog / key=value / text); the receiver runs the same parsers as the dashboard.
Standard library only, so it runs on any host with Python 3.9+.
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("src")))
from signal_sprint.live import simulate_batch  # noqa: E402


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
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tail", help="follow a log file")
    g.add_argument("--file", help="send a file once")
    g.add_argument("--simulate", action="store_true", help="generate synthetic traffic")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)
    if args.file:
        p = Path(args.file)
        print("status", post(args.url, args.key, p.read_bytes(), p.name, args.agent))
        return 0
    if args.tail:
        tail(Path(args.tail), args.url, args.key, args.agent, args.interval)
        return 0
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
