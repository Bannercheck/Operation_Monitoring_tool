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


def host_name() -> str:
    return socket.gethostname().split(".")[0] or "localhost"


def ensure_token(registry, settings_mod) -> str:
    """The self agent's token: kept (encrypted) in the settings; re-issued when the registry lost the agent or the token is gone."""
    cfg = settings_mod.load()
    name = host_name()
    rec = next((a for a in registry.list() if a["name"].lower() == name.lower()), None)
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
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
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
