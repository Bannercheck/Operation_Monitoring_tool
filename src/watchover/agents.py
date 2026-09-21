"""Agent identity: every collector gets its own token, a name, an environment and a site; tokens can be revoked.

Tokens are random (32 bytes, urlsafe) and only their sha256 is stored. The receiver resolves a token to the agent
record and stamps every event with the registered name / environment / site, so a mis-configured agent cannot post
under another host's name and ten servers never blend into one series. The legacy shared key keeps working (marked
`legacy`) so existing agents do not break on upgrade.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import secrets
from datetime import datetime, timezone

from .knowledge import Knowledge

UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AgentRegistry:
    def __init__(self, kb: Knowledge):
        self.kb = kb
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS agents (
            id {pk}, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, env TEXT DEFAULT '', site TEXT DEFAULT '', tags TEXT DEFAULT '',
            status TEXT DEFAULT 'active', created_at TEXT, last_seen TEXT DEFAULT '', last_ip TEXT DEFAULT '', events INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
        self._cache: dict[str, dict] = {}
        self._active: tuple[int, float] | None = None

    def enroll(self, name: str, env: str = "", site: str = "", tags: str = "", note: str = "") -> tuple[dict, str]:
        """Returns (agent record, plaintext token). The token is shown once and never stored."""
        if any(r["name"].lower() == name.strip().lower() and r["status"] == "active" for r in self.list()):
            raise ValueError(f"an active agent named '{name.strip()}' already exists: rotate its token instead of enrolling it twice")
        token = "wo_" + secrets.token_urlsafe(32)
        aid = self.kb._insert("INSERT INTO agents (name, token_hash, env, site, tags, status, created_at, note) VALUES (?,?,?,?,?,'active',?,?)",
                              (name.strip(), _hash(token), env.strip().lower(), site.strip(), tags.strip(), _now(), note.strip()))
        self._cache.clear(); self._active = None
        return self.get(aid), token

    # ---- fleet enrolment key: servers enrol themselves with it and receive their own token (no per-server copy/paste)
    def enroll_key(self, create: bool = True) -> str:
        """Current enrolment key ('wk_…'), created on first use; '' when disabled."""
        self.kb._exec("CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT)")
        rows = self.kb._exec("SELECT value FROM agent_settings WHERE key='enroll_key'")
        if rows:
            return rows[0]["value"] or ""
        if not create:
            return ""
        return self.rotate_enroll_key()

    def rotate_enroll_key(self) -> str:
        key = "wk_" + secrets.token_urlsafe(24)
        self._set_enroll_key(key); return key

    def disable_enroll_key(self) -> None:
        self._set_enroll_key("")

    def _set_enroll_key(self, value: str) -> None:
        self.kb._exec("CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT)")
        self.kb._exec("DELETE FROM agent_settings WHERE key='enroll_key'")
        self.kb._exec("INSERT INTO agent_settings (key, value) VALUES ('enroll_key', ?)", (value,))

    def self_enroll(self, key: str, name: str, env: str = "", site: str = "", tags: str = "") -> tuple[dict, str]:
        """Enrol a server that presents the fleet key. Raises PermissionError (bad/disabled key) or ValueError (name taken)."""
        cur = self.enroll_key(create=False)
        if not cur or not key or not hmac.compare_digest(key.encode(), cur.encode()):
            raise PermissionError("enrolment key is wrong or disabled")
        return self.enroll(name, env, site, tags, note="self-enrolled")

    def get(self, aid: int) -> dict | None:
        rows = self.kb._exec("SELECT * FROM agents WHERE id=?", (aid,))
        return dict(rows[0]) if rows else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM agents ORDER BY status, name")]

    def active_count(self) -> int:
        """Number of active agents (cached ~5 s: the receiver asks on every token-less request)."""
        now = time.time()
        if self._active is None or now - self._active[1] > 5:
            self._active = (int(self.kb._exec("SELECT COUNT(*) AS n FROM agents WHERE status='active'")[0]["n"]), now)
        return self._active[0]

    def verify(self, token: str | None) -> dict | None:
        """Active agent for this token, or None. Cached per hash (the receiver hits this on every batch)."""
        if not token:
            return None
        h = _hash(token)
        if h in self._cache:
            return self._cache[h]
        rows = self.kb._exec("SELECT * FROM agents WHERE token_hash=? AND status='active'", (h,))
        rec = dict(rows[0]) if rows else None
        if rec:
            self._cache[h] = rec
        return rec

    def revoke(self, aid: int) -> None:
        self.kb._exec("UPDATE agents SET status='revoked' WHERE id=?", (aid,))
        self._cache.clear(); self._active = None

    def reactivate(self, aid: int) -> None:
        self.kb._exec("UPDATE agents SET status='active' WHERE id=?", (aid,))
        self._cache.clear(); self._active = None

    def rotate(self, aid: int) -> str:
        token = "wo_" + secrets.token_urlsafe(32)
        self.kb._exec("UPDATE agents SET token_hash=?, status='active' WHERE id=?", (_hash(token), aid))
        self._cache.clear(); self._active = None
        return token

    def delete(self, aid: int) -> None:
        self.kb._exec("DELETE FROM agents WHERE id=?", (aid,))
        self._cache.clear(); self._active = None

    def touch(self, aid: int, ip: str, n: int) -> None:
        self.kb._exec("UPDATE agents SET last_seen=?, last_ip=?, events=events+? WHERE id=?", (_now(), ip, int(n), aid))
        for rec in self._cache.values():
            if rec["id"] == aid:
                rec["last_seen"] = _now(); rec["events"] = int(rec.get("events") or 0) + int(n)
