"""Watchover agent: ships host metrics and log lines from a server to the dashboard's live receiver.

    export WATCHOVER_TOKEN=wo_...                                   # the agent's own token (Connection settings › Agents)
    python3 agent.py --url http://<dashboard>:8600/ingest --metrics --tail /var/log/syslog --tail "/var/log/app/*.log"
    python3 agent.py --url http://<dashboard>:8600/ingest --test    # prove the address, port and token work, then exit
    python3 agent.py --url http://<dashboard>:8600/ingest --file export.csv   # one-shot upload

Standard library only: runs on any Linux / macOS host with Python 3.9+ (no Prometheus, no Zabbix, no pip). Any log
format works (JSON / JSONL / CSV / syslog / key=value / text); the receiver runs the same parsers as the dashboard.
With --spool DIR, batches that cannot be delivered are kept on disk and re-sent when the dashboard is reachable again.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.4.0"


def host_metrics() -> dict:
    """CPU / memory / disk percent using only the standard library (Linux / macOS; each field falls back gracefully)."""
    import shutil
    out = {"ts": datetime.now(timezone.utc).isoformat(), "host": socket.gethostname()}
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
        try:                                                   # macOS: vm_stat
            import subprocess
            r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            pages = {ln.split(":")[0].strip(): int(ln.split(":")[1].strip().rstrip(".")) for ln in r.stdout.splitlines() if ":" in ln and ln.split(":")[1].strip().rstrip(".").isdigit()}
            free = pages.get("Pages free", 0) + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)
            total = sum(v for k, v in pages.items() if k.startswith("Pages ") and k not in ("Pages purgeable", "Pages stored in compressor"))
            if total:
                out["memory"] = round(100.0 * (1 - free / total), 1)
        except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        pass
    return out


class Sender:
    """POSTs batches; keeps undeliverable batches in a disk spool and drains it once the receiver answers again."""

    def __init__(self, url: str, key: str | None, agent: str, spool: str | None = None, timeout: int = 15):
        self.url, self.key, self.agent, self.timeout = url, key, agent, timeout
        self.spool = Path(spool) if spool else None
        if self.spool:
            self.spool.mkdir(parents=True, exist_ok=True)
        self.sent = self.failed = self.spooled = 0

    def _post(self, data: bytes, name: str) -> dict:
        headers = {"Content-Type": "application/octet-stream", "X-Agent": self.agent, "X-File-Name": name, "X-Agent-Version": __version__}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"{}")

    def send(self, data: bytes, name: str) -> dict | None:
        try:
            out = self._post(data, name)
            self.sent += 1
            self.drain()
            return out
        except urllib.error.HTTPError as e:
            self.failed += 1
            if e.code in (401, 403):                           # a bad token never gets better by retrying: do not spool
                print(f"rejected by the receiver (HTTP {e.code}): check the token", file=sys.stderr)
                return None
            self._spool(data, name); return None
        except Exception as e:  # noqa: BLE001 - network down, DNS, timeout
            self.failed += 1
            print(f"send failed: {e}", file=sys.stderr)
            self._spool(data, name); return None

    def _spool(self, data: bytes, name: str) -> None:
        if not self.spool:
            return
        p = self.spool / f"{time.time_ns()}__{Path(name).name}"
        p.write_bytes(data); self.spooled += 1
        files = sorted(self.spool.glob("*__*"))
        for old in files[:-2000]:                              # bounded: keep the newest 2.000 batches
            old.unlink(missing_ok=True)

    def drain(self, limit: int = 200) -> int:
        """Re-send spooled batches oldest first; stops at the first failure."""
        if not self.spool:
            return 0
        n = 0
        for p in sorted(self.spool.glob("*__*"))[:limit]:
            try:
                self._post(p.read_bytes(), p.name.split("__", 1)[1])
                p.unlink(missing_ok=True); n += 1
            except Exception:  # noqa: BLE001
                break
        return n


def hello(sender: Sender, env: str) -> dict | None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": "info", "service": "watchover-agent", "host": sender.agent,
                       "env": env, "msg": f"agent {__version__} started on {socket.gethostname()}"}) + "\n"
    return sender.send(line.encode(), "agent.jsonl")


def tail_loop(pattern: str, sender: Sender, batch_secs: float, stop: threading.Event) -> None:
    """Follow every file matching the pattern (new matches are picked up every 30 s); starts at the end like tail -f."""
    handles: dict[str, object] = {}
    last_scan = 0.0
    buf: dict[str, list[str]] = {}
    last_flush = time.time()
    while not stop.is_set():
        if time.time() - last_scan > 30:
            for path in glob.glob(pattern) or ([pattern] if os.path.exists(pattern) else []):
                if path not in handles:
                    try:
                        f = open(path, "r", encoding="utf-8", errors="replace"); f.seek(0, 2); handles[path] = f
                        print(f"tailing {path}")
                    except OSError as e:
                        print(f"cannot open {path}: {e}", file=sys.stderr)
            last_scan = time.time()
        got = False
        for path, f in list(handles.items()):
            try:
                for _ in range(500):
                    line = f.readline()
                    if not line:
                        break
                    buf.setdefault(path, []).append(line); got = True
                if os.path.exists(path) and os.stat(path).st_ino != os.fstat(f.fileno()).st_ino:   # rotated: reopen
                    f.close(); handles.pop(path)
            except OSError:
                handles.pop(path, None)
        if buf and time.time() - last_flush >= batch_secs:
            for path, lines in list(buf.items()):
                sender.send("".join(lines).encode(), Path(path).name)
            buf, last_flush = {}, time.time()
        if not got:
            stop.wait(0.25)


def metrics_loop(sender: Sender, interval: float, env: str, stop: threading.Event) -> None:
    while not stop.is_set():
        m = host_metrics()
        if env:
            m["env"] = env
        sender.send((json.dumps(m) + "\n").encode(), "metrics.jsonl")
        stop.wait(max(interval, 1.0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("WATCHOVER_URL", "http://localhost:8600/ingest"), help="receiver URL (…/ingest); env WATCHOVER_URL")
    ap.add_argument("--key", "--token", dest="key", default=os.environ.get("WATCHOVER_TOKEN"), help="this agent's token (Connection settings › Agents) or the legacy shared key; env WATCHOVER_TOKEN")
    ap.add_argument("--agent", default=os.environ.get("WATCHOVER_AGENT") or socket.gethostname(), help="name sent as X-Agent (a registered token overrides it)")
    ap.add_argument("--env", default=os.environ.get("AGENT_ENV", ""), help="environment tag for this host (a registered token overrides it)")
    ap.add_argument("--tail", action="append", default=[], help="follow a log file or glob; repeatable (env WATCHOVER_LOGS, comma separated)")
    ap.add_argument("--metrics", action="store_true", help="ship this host's CPU / memory / disk / GPU every --interval seconds")
    ap.add_argument("--file", help="send a file once and exit")
    ap.add_argument("--simulate", action="store_true", help="generate synthetic traffic (needs the dashboard checkout)")
    ap.add_argument("--test", action="store_true", help="send one hello event, print the receiver's answer, exit")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("WATCHOVER_INTERVAL", "10")))
    ap.add_argument("--spool", default=os.environ.get("WATCHOVER_SPOOL", ""), help="directory for undeliverable batches (re-sent when the receiver is back)")
    args = ap.parse_args(argv)
    if os.environ.get("WATCHOVER_LOGS"):
        args.tail += [x.strip() for x in os.environ["WATCHOVER_LOGS"].split(",") if x.strip()]
    if os.environ.get("WATCHOVER_METRICS", "").lower() in ("1", "true", "yes"):
        args.metrics = True
    sender = Sender(args.url, args.key, args.agent, args.spool or None)
    if args.test:
        out = hello(sender, args.env)
        if out is None:
            print("FAILED: the receiver did not accept the hello event (address, port or token)"); return 2
        print(f"OK: receiver accepted · identity={out.get('identity')} · agent={out.get('agent')}"); return 0
    if args.file:
        p = Path(args.file)
        out = sender.send(p.read_bytes(), p.name)
        print("ok" if out else "failed", out or ""); return 0 if out else 2
    if args.simulate:
        sys.path.insert(0, str(Path(__file__).with_name("src")))
        from watchover.live import simulate_batch  # noqa: E402
        rng, tick = random.Random(), 0
        print(f"simulating -> {args.url} (Ctrl+C to stop)")
        while True:
            tick += 1
            sender.send(simulate_batch(rng, rng.randint(4, 12), 90 <= tick % 150 < 110), "sim.jsonl")
            time.sleep(max(args.interval, 0.2))
    if not args.tail and not args.metrics:
        ap.error("nothing to ship: use --metrics and/or --tail PATH (or --test / --file)")
    hello(sender, args.env)
    stop = threading.Event()
    threads = [threading.Thread(target=metrics_loop, args=(sender, args.interval, args.env, stop), daemon=True)] if args.metrics else []
    threads += [threading.Thread(target=tail_loop, args=(p, sender, 1.0, stop), daemon=True) for p in args.tail]
    for th in threads:
        th.start()
    print(f"watchover agent {__version__} -> {args.url} · metrics={'on' if args.metrics else 'off'} · tails={len(args.tail)} · spool={args.spool or 'off'} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(60)
            print(f"sent={sender.sent} failed={sender.failed} spooled={sender.spooled}", file=sys.stderr)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
