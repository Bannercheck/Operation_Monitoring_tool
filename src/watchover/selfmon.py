"""Monitoring of the machine Watchover itself runs on: the shipped agent.py is started as a child process against the local
receiver with its own enrolled token, shipping this host's CPU / memory / disk (and GPU where present), discovering and
following the application logs on this machine (--auto) and, on Linux with systemd, the journal. Real data from day one."""
from __future__ import annotations

import os
import shutil
import signal
import threading
import socket
import subprocess
import sys
import time
from pathlib import Path

AGENT_NAME_SUFFIX = "watchover-host"


def in_container() -> bool:
    return bool(os.environ.get("WATCHOVER_DOCKER") or os.environ.get("KUBERNETES_SERVICE_HOST")
                or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists())


def host_name() -> str:
    """The built-in agent's name and host. In a container the host name is the container / pod id, new at every re-create (each
    upgrade): the agent enrolled again under a fresh name every time. There it is WATCHOVER_SELF_NAME, else 'watchover-host'."""
    n = os.environ.get("WATCHOVER_SELF_NAME", "").strip()
    if n:
        return n[:64]
    if in_container():
        return AGENT_NAME_SUFFIX
    return socket.gethostname().split(".")[0] or "localhost"


def is_self(a: dict) -> bool:
    """A row of the built-in agent (enrol_token below): tag "self", env "local", site "watchover"."""
    return "self" in [t.strip() for t in str(a.get("tags") or "").split(",")] and a.get("env") == "local" and a.get("site") == "watchover"


def collapse(registry, name: str):
    """One built-in agent row: older versions enrolled a new one per container id (one per upgrade). The newest is kept and renamed
    to `name`; the others (never seen again) go. Returns the kept row or None."""
    mine = [a for a in registry.list() if is_self(a)]
    rec = next((a for a in mine if a["name"].lower() == name.lower()), None) \
        or max(mine, key=lambda a: str(a.get("last_seen") or a.get("created_at") or ""), default=None)
    for a in mine:
        if rec is not None and a["id"] != rec["id"]:
            registry.delete(a["id"])
    if rec is not None and rec["name"] != name and not any(a["name"].lower() == name.lower() for a in registry.list() if a["id"] != rec["id"]):
        registry.rename(rec["id"], name)
        rec = registry.get(rec["id"])
    return rec


def ensure_token(registry, settings_mod) -> str:
    """The self agent's token: kept (encrypted) in the settings; re-issued when the registry lost the agent or the token is gone."""
    cfg = settings_mod.load()
    name = host_name()
    rec = collapse(registry, name) or next((a for a in registry.list() if a["name"].lower() == name.lower()), None)
    tok = cfg.get("self_agent_token", "") or ""
    if rec and rec["status"] == "active" and tok and registry.verify(tok):
        return tok
    if rec:
        tok = registry.rotate(rec["id"])
    else:
        rec, tok = registry.enroll(name, env="local", site="watchover", tags="self", note="the machine Watchover runs on (built-in monitoring)")
    settings_mod.save({"self_agent_token": tok})
    return tok


def command(port: int, token: str, interval: int = 15, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[2]
    cmd = [sys.executable, str(root / "agent.py"), "--url", f"http://127.0.0.1:{port}/ingest", "--key", token, "--agent", host_name(), "--env", "local",
           "--auto", "--metrics", "--interval", str(interval)]
    if sys.platform.startswith("linux") and shutil.which("journalctl"):
        cmd.append("--journal")
    return cmd


def pid_file() -> Path:
    from . import settings
    return settings.home() / "self-agent.pid"


def _kill_stale() -> None:
    """An agent left behind by a previous application process (restart, crash) is ended before a new one starts: never two."""
    try:
        pid = int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace") if Path("/proc").exists() else "agent.py"
        if "agent.py" in cmd:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        pid_file().unlink()
    except OSError:
        pass


class SelfMonitor:
    """Owns the child process; start() is idempotent and serialised, ensure() restarts it if it died, stop() ends it.
    The child's pid is kept in data/self-agent.pid so that a restarted application replaces, never duplicates, the agent."""

    _lock = threading.Lock()

    def __init__(self, port: int, token: str, interval: int = 15, log: str = ""):
        self.port, self.token, self.interval, self.log = port, token, interval, log
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.restarts = 0

    def start(self) -> "SelfMonitor":
        with self._lock:
            if self.running:
                return self
            _kill_stale()
            out = open(self.log, "ab") if self.log else subprocess.DEVNULL
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "WATCHOVER_HOSTNAME": host_name()}   # the host its lines and metrics carry
            self.proc = subprocess.Popen(command(self.port, self.token, self.interval), stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            self.started = time.time()
            try:
                pid_file().write_text(str(self.proc.pid))
            except OSError:
                pass
            return self

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> bool:
        if not self.running and self.proc is not None and time.time() - self.started > 20:
            self.restarts += 1
            self.start()
        return self.running

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        try:
            pid_file().unlink()
        except OSError:
            pass

    def status(self) -> dict:
        return {"running": self.running, "pid": self.proc.pid if self.running else None, "since": self.started, "restarts": self.restarts, "host": host_name()}
