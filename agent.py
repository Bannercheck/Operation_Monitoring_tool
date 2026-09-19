"""Watchover agent: ships host metrics and log lines from a server to the dashboard's live receiver.

    export WATCHOVER_TOKEN=wo_...                                   # the agent's own token (Connection settings › Agents)
    python3 agent.py --url http://<dashboard>:8600/ingest --metrics --tail /var/log/syslog --tail "/var/log/app/*.log"
    python3 agent.py --url http://<dashboard>:8600/ingest --test    # prove the address, port and token work, then exit
    python3 agent.py --url http://<dashboard>:8600/ingest --file export.csv   # one-shot upload

    python3 agent.py --url http://<dashboard>:8600/ingest --auto --metrics   # discover SAP / Oracle / EBS / Java / web / db / container logs and follow them
    python3 agent.py --discover                                              # only print what --auto would follow

Standard library only: runs on any Linux / macOS / Windows host with Python 3.9+ (no Prometheus, no Zabbix, no pip). Any log
format works (JSON / JSONL / CSV / syslog / key=value / text); the receiver runs the same parsers as the dashboard.
With --spool DIR, batches that cannot be delivered are kept on disk and re-sent when the dashboard is reachable again.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.5.0"


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
            total = sum(pages.get(k, 0) for k in ("Pages free", "Pages active", "Pages inactive", "Pages speculative", "Pages wired down", "Pages occupied by compressor"))
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

    PERMANENT = {400, 404, 405, 413, 415, 422}                  # the receiver will never accept this exact batch: drop it

    def __init__(self, url: str, key: str | None, agent: str, spool: str | None = None, timeout: int = 15):
        self.url, self.key, self.agent, self.timeout = url, key, agent, timeout
        self.spool = Path(spool) if spool else None
        self._lock = threading.Lock()
        if self.spool:
            self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.spool, 0o700)                    # spooled batches may hold privileged log lines
            except OSError:
                pass
        self.sent = self.failed = self.spooled = self.dropped = 0

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
            if e.code in self.PERMANENT:                       # the receiver refused the content itself: retrying cannot help
                self.dropped += 1
                print(f"batch {name} rejected (HTTP {e.code}): dropped", file=sys.stderr)
                return None
            self._spool(data, name); return None
        except Exception as e:  # noqa: BLE001 - network down, DNS, timeout
            self.failed += 1
            print(f"send failed: {e}", file=sys.stderr)
            self._spool(data, name); return None

    def _spool(self, data: bytes, name: str) -> None:
        if not self.spool:
            return
        try:
            p = self.spool / f"{time.time_ns()}__{Path(name).name}"
            p.write_bytes(data); os.chmod(p, 0o600); self.spooled += 1
            files = sorted(self.spool.glob("*__*"))
            for old in files[:-2000]:                          # bounded: keep the newest 2.000 batches
                old.unlink(missing_ok=True)
        except OSError as e:                                   # disk full / permissions: losing this batch beats killing the thread
            self.dropped += 1
            print(f"spool write failed: {e}", file=sys.stderr)

    def drain(self, limit: int = 200) -> int:
        """Re-send spooled batches oldest first; a permanently rejected batch is dropped, a network failure stops the drain."""
        if not self.spool or not self._lock.acquire(blocking=False):   # one thread drains at a time (no duplicate re-sends)
            return 0
        n = 0
        try:
            for p in sorted(self.spool.glob("*__*"))[:limit]:
                try:
                    self._post(p.read_bytes(), p.name.split("__", 1)[1])
                    p.unlink(missing_ok=True); n += 1
                except urllib.error.HTTPError as e:
                    if e.code in self.PERMANENT or e.code in (401, 403):
                        p.unlink(missing_ok=True); self.dropped += 1
                        if e.code in (401, 403):
                            break
                        continue
                    break
                except Exception:  # noqa: BLE001
                    break
        finally:
            self._lock.release()
        return n


def hello(sender: Sender, env: str) -> dict | None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": "info", "service": "watchover-agent", "host": sender.agent,
                       "env": env, "msg": f"agent {__version__} started on {socket.gethostname()}"}) + "\n"
    return sender.send(line.encode(), "agent.jsonl")


def tail_loop(pattern: str, sender: Sender, batch_secs: float, stop: threading.Event) -> None:
    """Follow every file matching the pattern like `tail -F`: starts at the end, survives rename rotation (logrotate,
    newsyslog) and in-place truncation (copytruncate), ships only complete lines, re-scans the glob every 30 s."""
    handles: dict[str, object] = {}
    seen: set[str] = set()                                     # paths opened before: a re-appearing path is read from the start
    partial: dict[str, str] = {}                               # trailing fragment without newline, per path
    buf: dict[str, list[str]] = {}
    last_scan = 0.0
    last_flush = time.time()
    warned = False
    while not stop.is_set():
        if time.time() - last_scan > 30:
            paths = glob.glob(pattern) or ([pattern] if os.path.exists(pattern) else [])
            if not paths and not warned:
                print(f"no file matches {pattern} yet (waiting)", file=sys.stderr); warned = True
            for path in paths:
                if path not in handles:
                    try:
                        f = open(path, "r", encoding="utf-8", errors="replace")
                        if path not in seen:
                            f.seek(0, 2)                       # first sight: start at the end like tail -f
                        seen.add(path); handles[path] = f
                        print(f"tailing {path}")
                    except OSError as e:
                        print(f"cannot open {path}: {e}", file=sys.stderr)
            last_scan = time.time()
        got = False
        for path, f in list(handles.items()):
            try:
                try:
                    if os.fstat(f.fileno()).st_size < f.tell():   # truncated in place (copytruncate): start over
                        f.seek(0); partial.pop(path, None)
                except (OSError, ValueError):
                    pass
                for _ in range(500):
                    line = f.readline()
                    if not line:
                        break
                    got = True
                    if path in partial:
                        line = partial.pop(path) + line
                    if line.endswith("\n"):
                        buf.setdefault(path, []).append(line)
                    else:                                      # the writer has not finished this line: wait for the rest
                        partial[path] = line
                        break
                rotated = False
                try:
                    rotated = os.path.exists(path) and os.stat(path).st_ino != os.fstat(f.fileno()).st_ino
                except OSError:
                    rotated = True
                if rotated:                                    # renamed: old handle is drained above; reopen the new file now
                    f.close(); handles.pop(path); last_scan = 0.0     # stays in `seen`: the new file is read from its start
                    if path in partial:                        # a fragment left in the rotated file is the last line
                        buf.setdefault(path, []).append(partial.pop(path) + "\n")
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
        try:
            m = host_metrics()
            if env:
                m["env"] = env
            sender.send((json.dumps(m) + "\n").encode(), "metrics.jsonl")
        except Exception as e:  # noqa: BLE001
            print(f"metrics failed: {e}", file=sys.stderr)
        stop.wait(max(interval, 1.0))


# ---------------------------------------------------------------- automatic log discovery (Linux / macOS / Windows / Kubernetes nodes)
PROFILES = [
    # name, detector (any path exists), log globs
    ("SAP NetWeaver ABAP", ["/usr/sap/*/D*/work", "/usr/sap/*/DVEBMGS*/work", "/usr/sap/*/ASCS*/work"],
     ["/usr/sap/*/D*/work/dev_w*", "/usr/sap/*/D*/work/dev_disp", "/usr/sap/*/D*/work/dev_rfc*", "/usr/sap/*/D*/work/available.log", "/usr/sap/*/DVEBMGS*/work/dev_w*",
      "/usr/sap/*/DVEBMGS*/work/dev_disp", "/usr/sap/*/ASCS*/work/dev_ms", "/usr/sap/*/ASCS*/work/dev_enq*", "/usr/sap/*/*/work/sapstart.log", "/usr/sap/*/*/work/available.log"]),
    ("SAP NetWeaver Java", ["/usr/sap/*/J*/j2ee/cluster", "/usr/sap/*/J*/work"],
     ["/usr/sap/*/J*/j2ee/cluster/server*/log/defaultTrace*.trc", "/usr/sap/*/J*/j2ee/cluster/server*/log/applications*.log", "/usr/sap/*/J*/work/dev_server*", "/usr/sap/*/J*/work/*.jvm", "/usr/sap/*/J*/work/std_server*.out"]),
    ("SAP HANA", ["/usr/sap/*/HDB*", "/hana/shared/*/HDB*"],
     ["/usr/sap/*/HDB*/*/trace/indexserver_*.trc", "/usr/sap/*/HDB*/*/trace/nameserver_*.trc", "/usr/sap/*/HDB*/*/trace/xsengine_*.trc", "/usr/sap/*/HDB*/*/trace/*alert*.trc", "/usr/sap/*/HDB*/*/trace/daemon_*.trc"]),
    ("SAP Host Agent", ["/usr/sap/hostctrl"], ["/usr/sap/hostctrl/work/dev_saphostexec", "/usr/sap/hostctrl/work/sapstartsrv.log"]),
    ("Oracle Database", ["/u01/app/oracle/diag/rdbms", "/opt/oracle/diag/rdbms", "/oracle/*/diag/rdbms", "$ORACLE_BASE/diag/rdbms"],
     ["/u01/app/oracle/diag/rdbms/*/*/trace/alert_*.log", "/opt/oracle/diag/rdbms/*/*/trace/alert_*.log", "/oracle/*/diag/rdbms/*/*/trace/alert_*.log", "$ORACLE_BASE/diag/rdbms/*/*/trace/alert_*.log",
      "/u01/app/oracle/diag/tnslsnr/*/listener/trace/listener.log", "$ORACLE_BASE/diag/tnslsnr/*/*/trace/*.log"]),
    ("Oracle E-Business Suite", ["/u01/*/inst/apps", "/u01/install/APPS/inst/apps", "$INST_TOP/logs", "/oracle/*/inst/apps"],
     ["/u01/*/inst/apps/*/logs/appl/conc/log/*.log", "/u01/*/inst/apps/*/logs/appl/conc/log/*.mgr", "/u01/*/inst/apps/*/logs/appl/rgf/*.log", "/u01/*/inst/apps/*/logs/ora/10.1.3/opmn/*.log",
      "/u01/*/inst/apps/*/logs/ora/10.1.3/j2ee/oacore/*.log", "$INST_TOP/logs/appl/conc/log/*.log", "$INST_TOP/logs/ora/10.1.3/opmn/*.log", "/u01/*/fs1/inst/apps/*/logs/appl/conc/log/*.log", "/u01/*/fs2/inst/apps/*/logs/appl/conc/log/*.log"]),
    ("Oracle WebLogic", ["/u01/*/user_projects/domains", "/opt/oracle/*/user_projects/domains"],
     ["/u01/*/user_projects/domains/*/servers/*/logs/*.log", "/u01/*/user_projects/domains/*/servers/*/logs/*.out", "/opt/oracle/*/user_projects/domains/*/servers/*/logs/*.log"]),
    ("Apache Tomcat", ["/opt/tomcat*", "/usr/share/tomcat*", "/var/log/tomcat*"], ["/opt/tomcat*/logs/catalina.out", "/opt/tomcat*/logs/*.log", "/usr/share/tomcat*/logs/*.log", "/var/log/tomcat*/*.log", "/var/log/tomcat*/catalina.out"]),
    ("JBoss / WildFly", ["/opt/wildfly*", "/opt/jboss*"], ["/opt/wildfly*/standalone/log/server.log", "/opt/jboss*/standalone/log/server.log", "/opt/wildfly*/domain/servers/*/log/server.log"]),
    ("IBM WebSphere", ["/opt/IBM/WebSphere"], ["/opt/IBM/WebSphere/AppServer/profiles/*/logs/*/SystemOut.log", "/opt/IBM/WebSphere/AppServer/profiles/*/logs/*/SystemErr.log"]),
    ("nginx", ["/var/log/nginx"], ["/var/log/nginx/*.log"]),
    ("Apache httpd", ["/var/log/httpd", "/var/log/apache2"], ["/var/log/httpd/*log", "/var/log/apache2/*.log"]),
    ("PostgreSQL", ["/var/log/postgresql", "/var/lib/pgsql"], ["/var/log/postgresql/*.log", "/var/lib/pgsql/*/data/log/*.log", "/var/lib/pgsql/data/log/*.log"]),
    ("MySQL / MariaDB", ["/var/log/mysql", "/var/log/mariadb"], ["/var/log/mysql/*.log", "/var/log/mariadb/*.log", "/var/log/mysqld.log"]),
    ("MongoDB", ["/var/log/mongodb"], ["/var/log/mongodb/*.log"]),
    ("Redis", ["/var/log/redis"], ["/var/log/redis/*.log"]),
    ("Kafka", ["/opt/kafka*", "/var/log/kafka"], ["/opt/kafka*/logs/server.log", "/var/log/kafka/*.log"]),
    ("Elasticsearch", ["/var/log/elasticsearch"], ["/var/log/elasticsearch/*.log"]),
    ("Docker containers", ["/var/lib/docker/containers"], ["/var/lib/docker/containers/*/*-json.log"]),
    ("Kubernetes node", ["/var/log/containers", "/var/log/pods", "/var/lib/kubelet"], ["/var/log/containers/*.log", "/var/log/kube-apiserver.log", "/var/log/kube-scheduler.log", "/var/log/kubelet.log"]),
    ("Linux system", ["/var/log"], ["/var/log/syslog", "/var/log/messages", "/var/log/auth.log", "/var/log/secure", "/var/log/kern.log", "/var/log/cron", "/var/log/dmesg"]),
    ("macOS system", ["/var/log/system.log"], ["/var/log/system.log", "/var/log/install.log"]),
    ("Windows IIS", [r"C:\inetpub\logs\LogFiles"], [r"C:\inetpub\logs\LogFiles\*\*.log"]),
    ("SAP on Windows", [r"*:\usr\sap"], [r"*:\usr\sap\*\D*\work\dev_w*", r"*:\usr\sap\*\D*\work\dev_disp", r"*:\usr\sap\*\*\work\available.log", r"*:\usr\sap\*\J*\j2ee\cluster\server*\log\defaultTrace*.trc"]),
    ("Oracle on Windows", [r"*:\app\*\diag\rdbms", r"*:\oracle\*\diag\rdbms"], [r"*:\app\*\diag\rdbms\*\*\trace\alert_*.log", r"*:\oracle\*\diag\rdbms\*\*\trace\alert_*.log"]),
    ("Windows applications", [r"C:\ProgramData"], [r"C:\ProgramData\*\logs\*.log", r"C:\ProgramData\*\log\*.log"]),
]
MAX_AUTO_FILES = 200


def _expand(pattern: str) -> list[str]:
    pattern = os.path.expandvars(pattern)
    if "$" in pattern:
        return []
    if pattern.startswith("*:"):                                # every Windows drive letter
        out = []
        for d in "CDEFGH":
            out += glob.glob(f"{d}:" + pattern[2:])
        return out
    return glob.glob(pattern)


def discover_logs() -> list[dict]:
    """Applications present on this host and the log files they write, newest-modified first, capped per profile."""
    found = []
    for name, markers, globs in PROFILES:
        if not any(_expand(m) for m in markers):   # detector first: cheap check before the globs
            continue
        files: dict[str, float] = {}
        for g in globs:
            for f in _expand(g):
                try:
                    if os.path.isfile(f) and os.access(f, os.R_OK):
                        files[f] = os.path.getmtime(f)
                except OSError:
                    pass
        if files:
            ordered = sorted(files, key=files.get, reverse=True)
            found.append({"app": name, "files": ordered[:40], "skipped": max(0, len(ordered) - 40)})
    return found


def discovery_report(sender: "Sender", env: str, found: list[dict]) -> None:
    """One informational event per detected application so the dashboard can show what this server ships."""
    if not found:
        return
    lines = []
    for f in found:
        lines.append(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": "info", "service": "watchover-agent", "host": sender.agent, "env": env,
                                 "msg": f"discovered {f['app']}: {len(f['files'])} log files" + (f" (+{f['skipped']} older skipped)" if f["skipped"] else ""),
                                 "discovery_app": f["app"], "discovery_files": f["files"][:40]}, ensure_ascii=False))
    sender.send(("\n".join(lines) + "\n").encode(), "discovery.jsonl")


def journal_loop(sender: "Sender", batch_secs: float, stop: threading.Event) -> None:
    """systemd journal as a stream (hosts without /var/log/syslog): journalctl -f -o json."""
    import subprocess
    try:
        proc = subprocess.Popen(["journalctl", "-f", "-n", "0", "-o", "json"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return
    buf: list[str] = []
    last = time.time()
    while not stop.is_set():
        line = proc.stdout.readline()
        if line:
            try:
                j = json.loads(line)
                buf.append(json.dumps({"ts": datetime.fromtimestamp(int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6, tz=timezone.utc).isoformat(),
                                       "level": {0: "critical", 1: "critical", 2: "critical", 3: "error", 4: "warn"}.get(int(j.get("PRIORITY", 6)), "info"),
                                       "service": j.get("SYSLOG_IDENTIFIER") or j.get("_COMM") or "journal", "host": j.get("_HOSTNAME", sender.agent), "msg": str(j.get("MESSAGE", ""))[:2000]}))
            except (ValueError, TypeError):
                pass
        if buf and time.time() - last >= batch_secs:
            sender.send(("\n".join(buf) + "\n").encode(), "journal.jsonl"); buf, last = [], time.time()
        if not line:
            stop.wait(0.2)
    proc.terminate()


def windows_events_loop(sender: "Sender", interval: float, stop: threading.Event, channels=("System", "Application")) -> None:
    """Windows Event Log through wevtutil (no extra packages): new Warning / Error / Critical records every interval."""
    import subprocess
    import xml.etree.ElementTree as ET
    seen: dict[str, str] = {}
    while not stop.is_set():
        out_lines = []
        for ch in channels:
            try:
                r = subprocess.run(["wevtutil", "qe", ch, "/c:100", "/rd:true", "/f:xml", "/q:*[System[(Level<=3)]]"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            newest = None
            for m in re.finditer(r"<Event .*?</Event>", r.stdout, flags=re.S):
                try:
                    ev = ET.fromstring(m.group(0))
                    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
                    sysn = ev.find("e:System", ns)
                    rid = sysn.findtext("e:EventRecordID", default="", namespaces=ns)
                    if newest is None:
                        newest = rid
                    if seen.get(ch) and rid <= seen[ch]:
                        break
                    lvl = {"1": "critical", "2": "error", "3": "warn"}.get(sysn.findtext("e:Level", default="4", namespaces=ns), "info")
                    tc = sysn.find("e:TimeCreated", ns)
                    prov = sysn.find("e:Provider", ns)
                    msg = " ".join(d.text or "" for d in ev.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Data"))[:2000]
                    out_lines.append(json.dumps({"ts": (tc.get("SystemTime") if tc is not None else datetime.now(timezone.utc).isoformat()), "level": lvl,
                                                 "service": (prov.get("Name") if prov is not None else ch), "host": sender.agent, "msg": msg or f"{ch} event {sysn.findtext('e:EventID', default='', namespaces=ns)}",
                                                 "channel": ch, "event_id": sysn.findtext("e:EventID", default="", namespaces=ns)}, ensure_ascii=False))
                except ET.ParseError:
                    continue
            if newest:
                seen[ch] = newest
        if out_lines:
            sender.send(("\n".join(reversed(out_lines)) + "\n").encode(), "windows-events.jsonl")
        stop.wait(max(interval, 5.0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("WATCHOVER_URL", "http://localhost:8600/ingest"), help="receiver URL (…/ingest); env WATCHOVER_URL")
    ap.add_argument("--key", "--token", dest="key", default=os.environ.get("WATCHOVER_TOKEN"), help="this agent's token (Connection settings › Agents) or the legacy shared key; env WATCHOVER_TOKEN")
    ap.add_argument("--agent", default=os.environ.get("WATCHOVER_AGENT") or socket.gethostname(), help="name sent as X-Agent (a registered token overrides it)")
    ap.add_argument("--env", default=os.environ.get("AGENT_ENV", ""), help="environment tag for this host (a registered token overrides it)")
    ap.add_argument("--tail", action="append", default=[], help="follow a log file or glob; repeatable (env WATCHOVER_LOGS, comma separated)")
    ap.add_argument("--auto", action="store_true", help="discover the applications on this host (SAP, Oracle, EBS, Java servers, web, databases, containers, Kubernetes, system) and follow their logs; env WATCHOVER_LOGS=auto")
    ap.add_argument("--discover", action="store_true", help="print what --auto would follow and exit")
    ap.add_argument("--journal", action="store_true", help="also stream the systemd journal (Linux)")
    ap.add_argument("--windows-events", action="store_true", help="also poll the Windows Event Log (System, Application)")
    ap.add_argument("--metrics", action="store_true", help="ship this host's CPU / memory / disk / GPU every --interval seconds")
    ap.add_argument("--file", help="send a file once and exit")
    ap.add_argument("--simulate", action="store_true", help="generate synthetic traffic (needs the dashboard checkout)")
    ap.add_argument("--test", action="store_true", help="send one hello event, print the receiver's answer, exit")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("WATCHOVER_INTERVAL", "10")))
    ap.add_argument("--spool", default=os.environ.get("WATCHOVER_SPOOL", ""), help="directory for undeliverable batches (re-sent when the receiver is back)")
    args = ap.parse_args(argv)
    if os.environ.get("WATCHOVER_LOGS"):
        for x in os.environ["WATCHOVER_LOGS"].split(","):
            x = x.strip()
            if x == "auto":
                args.auto = True
            elif x == "journal":
                args.journal = True
            elif x == "windows-events":
                args.windows_events = True
            elif x:
                args.tail.append(x)
    if args.discover:
        for f in discover_logs():
            print(f"{f['app']}: {len(f['files'])} files" + (f" (+{f['skipped']} older)" if f["skipped"] else ""))
            for path in f["files"]:
                print(f"    {path}")
        return 0
    found = discover_logs() if args.auto else []
    if args.auto:
        auto_files = [p for f in found for p in f["files"]][:MAX_AUTO_FILES]
        args.tail += [p for p in auto_files if p not in args.tail]
        if sys.platform.startswith("win"):
            args.windows_events = True
        elif not any(p in ("/var/log/syslog", "/var/log/messages") for p in args.tail) and os.path.exists("/run/systemd/system"):
            args.journal = True
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
    if not args.tail and not args.metrics and not args.journal and not args.windows_events:
        ap.error("nothing to ship: use --metrics, --auto and/or --tail PATH (or --test / --file)")
    hello(sender, args.env)
    if found:
        discovery_report(sender, args.env, found)
    stop = threading.Event()
    threads = [threading.Thread(target=metrics_loop, args=(sender, args.interval, args.env, stop), daemon=True)] if args.metrics else []
    threads += [threading.Thread(target=tail_loop, args=(p, sender, 1.0, stop), daemon=True) for p in args.tail]
    if args.journal:
        threads.append(threading.Thread(target=journal_loop, args=(sender, 1.0, stop), daemon=True))
    if args.windows_events:
        threads.append(threading.Thread(target=windows_events_loop, args=(sender, max(args.interval, 5.0), stop), daemon=True))
    for th in threads:
        th.start()
    apps = ", ".join(f"{f['app']} ({len(f['files'])})" for f in found) if found else "-"
    print(f"watchover agent {__version__} -> {args.url} · metrics={'on' if args.metrics else 'off'} · tails={len(args.tail)} · auto={apps} · spool={args.spool or 'off'} (Ctrl+C to stop)")
    try:
        while True:
            for _ in range(60):
                time.sleep(1)
                if not any(th.is_alive() for th in threads):
                    print("all workers stopped: exiting so the service manager restarts the agent", file=sys.stderr); return 3
            print(f"sent={sender.sent} failed={sender.failed} spooled={sender.spooled} dropped={sender.dropped}", file=sys.stderr)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
