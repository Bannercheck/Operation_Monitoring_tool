"""The Watchover server's own agent: install-rhel.sh installs the same agent every monitored server gets on the host that runs
Watchover, so its journal, logs and metrics are there from the first minute. This prints the token for it, run inside the
dashboard container (it reaches the database): a new agent named after the host, or a fresh token for the one enrolled by an
earlier install (enrolling a name twice is refused), never the fleet key.

    python -m watchover.selfagent token --name watchover01 [--env prod]   -> the token on stdout (shown once, stored hashed)
    python -m watchover.selfagent release-key                            -> SHA256:… of keyd's release key (empty: none yet)
    python -m watchover.selfagent builtin-off                            -> the built-in in-container agent (selfmon) stopped
                                                                            and its rows removed: the host agent replaces it
"""
from __future__ import annotations

import argparse
import os
import sys


def token(name: str, env: str = "prod") -> str:
    reg = _registry()
    for r in reg.list():
        if r["name"].lower() == name.strip().lower():
            if r["status"] != "active":
                reg.reactivate(r["id"])
            return reg.rotate(r["id"])                       # a re-run (repair, upgrade): the same agent row, a new token
    _agent, tok = reg.enroll(name, env=env, tags="watchover-server", note="the Watchover server itself (install-rhel.sh)")
    return tok


def _registry():
    from .agents import AgentRegistry
    from .knowledge import Knowledge
    return AgentRegistry(Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db")))


def release_key() -> str:
    """The fingerprint the Agents panel's install command pins (--release-key): the agent verifies the signed manifest and
    its own later self-updates against it."""
    from .act.keys import KeyClient
    from .agent_update import AgentUpdates
    rel = AgentUpdates(_registry(), lambda: KeyClient(None)).release(force=True)
    return (rel or {}).get("fp", "")


def builtin_off() -> int:
    """The in-container agent sees the container, not the server (no host journal, the container's own CPU / disk view): once
    the real agent runs on the host it is redundant and a second row for the same machine. Its child process is ended and its
    rows go; WATCHOVER_SELFMON=0 in .env (install-rhel.sh) keeps it off after the next start. Returns the rows removed."""
    from . import selfmon
    selfmon._kill_stale()
    reg = _registry()
    gone = 0
    for a in reg.list():
        if selfmon.is_self(a):
            reg.delete(a["id"])
            gone += 1
    return gone


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m watchover.selfagent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("token", help="token for the host's own agent")
    t.add_argument("--name", required=True)
    t.add_argument("--env", default="prod")
    sub.add_parser("release-key", help="fingerprint of keyd's release key")
    sub.add_parser("builtin-off", help="stop the built-in in-container agent and remove its rows")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "token":
            print(token(a.name, a.env))
        elif a.cmd == "release-key":
            print(release_key())
        else:
            print(builtin_off())
    except ValueError as e:
        print(f"selfagent: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
