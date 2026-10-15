"""MCP access keys (Ayarlar › Bağlantılar › MCP) and the audit of every MCP tool call.

A key is a named, revocable secret bound either to one account (it sees exactly what that account sees, never more) or to one
team (that team's hosts and service rules only). It is read-only unless created with the write ("yazma") permission; even then a
write tool also needs the underlying permission (act.anomaly, act.thresholds / sys.notify, page.actions). Only a SHA-256 of the
key is stored and the clear key is shown once. MCP_API_KEY (environment) stays valid as an installation-wide read key
(MCP_API_KEY_WRITE=1 restores the old write access for create_action).

Every call lands in mcp_audit: when, which key, who (account / team / installation), tool, scope, result, duration and how many
items came back; argument names only, never their values (a log query or a host name can carry personal data)."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from .access import Access, Membership

UTC = timezone.utc
PREFIX = "wom_"
READ_PERMS = frozenset({"page.ops", "page.logs", "page.thr", "page.anom", "page.apm", "page.inv", "page.actions"})
WRITE_PERMS = frozenset({"act.anomaly", "act.thresholds", "sys.notify"})
TEAM_WRITE = frozenset({"act.anomaly", "act.thresholds"})
AUDIT_DAYS = 90
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def digest(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


@dataclass
class Identity:
    """Who is calling: the key, the principal it acts for, the (restricted) Access and whether it may write."""
    key_id: Optional[int]
    name: str
    principal: str            # account e-mail, "team:<name>" or "install"
    scope: str                # "all", "user:<email>" or "team:<name>"
    acc: Access
    write: bool
    team_id: Optional[int] = None

    @property
    def user(self) -> dict:
        return {"id": None, "email": self.acc.email, "name": f"MCP: {self.name}", "role": self.acc.role}


def restrict(acc: Access, allowed: frozenset) -> Access:
    """The same account and team scopes, with permissions narrowed to `allowed` (an MCP key never exceeds its owner)."""
    ms = tuple(replace(m, perms=frozenset(m.perms & allowed)) for m in acc.memberships)
    g = frozenset(acc.global_perms & allowed)
    perms = frozenset(acc.perms & allowed)
    return Access(email=acc.email, role="mcp", all=acc.all, strict=acc.strict, global_perms=g, memberships=ms, perms=perms or frozenset({"-"}))


def install_identity(write: bool = False) -> Identity:
    perms = READ_PERMS | (WRITE_PERMS if write else frozenset())
    acc = Access(email="mcp-api-key@mcp", role="mcp", all=True, strict=False, global_perms=perms, memberships=())
    return Identity(None, "MCP_API_KEY", "install", "all", acc, write)


def team_identity(team: dict, key_id: Optional[int], name: str, write: bool) -> Identity:
    perms = READ_PERMS | (TEAM_WRITE if write else frozenset())
    m = Membership(team_id=team["id"], team=team["name"], role_id=None, role_key="mcp", role_label="MCP", perms=frozenset(perms),
                   hosts=frozenset(team.get("matched") or ()), service_rules=tuple(x for x in team.get("rules") or () if x.get("field") == "service"))
    acc = Access(email=f"mcp-key-{key_id}@mcp", role="mcp", all=False, strict=True, global_perms=frozenset(), memberships=(m,))
    return Identity(key_id, name, f"team:{team['name']}", f"team:{team['name']}", acc, write, team["id"])


class McpKeys:
    COLS = ("id", "name", "prefix", "kind", "email", "team_id", "can_write", "created_by", "created_at", "last_used", "calls", "revoked_at", "revoked_by", "note")

    def __init__(self, kb):
        self.kb = kb
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS mcp_keys (id {pk}, name TEXT NOT NULL, prefix TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            email TEXT DEFAULT '', team_id INTEGER, can_write INTEGER DEFAULT 0, created_by TEXT DEFAULT '', created_at TEXT, last_used TEXT DEFAULT '',
            calls INTEGER DEFAULT 0, revoked_at TEXT DEFAULT '', revoked_by TEXT DEFAULT '', note TEXT DEFAULT '')""")
        kb._exec(f"""CREATE TABLE IF NOT EXISTS mcp_audit (id {pk}, ts TEXT NOT NULL, direction TEXT DEFAULT 'in', key_id INTEGER, key_name TEXT DEFAULT '',
            principal TEXT DEFAULT '', scope TEXT DEFAULT '', tool TEXT NOT NULL, args TEXT DEFAULT '', ok INTEGER DEFAULT 1, error TEXT DEFAULT '',
            ms INTEGER DEFAULT 0, items INTEGER DEFAULT 0, team_id INTEGER)""")
        self._n = 0

    # ---- keys
    def _out(self, r: dict) -> dict:
        d = {k: r.get(k) for k in self.COLS}
        d["write"] = bool(d.pop("can_write"))
        d["active"] = not d.get("revoked_at")
        return d

    def create(self, name: str, kind: str, email: str = "", team_id: Optional[int] = None, write: bool = False, created_by: str = "",
               note: str = "") -> tuple[dict, str]:
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("name is required")
        if kind not in ("user", "team"):
            raise ValueError("kind must be user or team")
        if kind == "user" and not email:
            raise ValueError("email is required for a user key")
        if kind == "team" and team_id is None:
            raise ValueError("team_id is required for a team key")
        clear = PREFIX + secrets.token_urlsafe(32)
        kid = self.kb._insert("""INSERT INTO mcp_keys (name, prefix, hash, kind, email, team_id, can_write, created_by, created_at, note)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (name, clear[:10], digest(clear), kind, (email or "").lower() if kind == "user" else "",
                                             team_id if kind == "team" else None, int(bool(write)), (created_by or "").lower(), _now(), (note or "")[:200]))
        return self.get(kid), clear

    def get(self, kid: int) -> dict:
        rows = self.kb._exec("SELECT * FROM mcp_keys WHERE id=?", (kid,))
        if not rows:
            raise KeyError(f"mcp key {kid}")
        return self._out(dict(rows[0]))

    def list(self) -> list:
        return [self._out(dict(r)) for r in self.kb._exec("SELECT * FROM mcp_keys ORDER BY id DESC")]

    def revoke(self, kid: int, by: str = "") -> dict:
        self.get(kid)
        self.kb._exec("UPDATE mcp_keys SET revoked_at=?, revoked_by=? WHERE id=? AND (revoked_at IS NULL OR revoked_at='')", (_now(), (by or "").lower(), kid))
        return self.get(kid)

    def revoke_team(self, team_id: int, by: str = "team deleted") -> None:
        self.kb._exec("UPDATE mcp_keys SET revoked_at=?, revoked_by=? WHERE team_id=? AND (revoked_at IS NULL OR revoked_at='')", (_now(), by, team_id))

    def verify(self, clear: str) -> Optional[dict]:
        """The active key row for a presented clear key, or None."""
        if not clear or not clear.startswith(PREFIX) or len(clear) > 200:
            return None
        rows = self.kb._exec("SELECT * FROM mcp_keys WHERE hash=?", (digest(clear),))
        if not rows or rows[0].get("revoked_at"):
            return None
        return dict(rows[0])

    def touch(self, kid: int) -> None:
        try:
            self.kb._exec("UPDATE mcp_keys SET last_used=?, calls=calls+1 WHERE id=?", (_now(), kid))
        except Exception:  # noqa: BLE001 - bookkeeping never blocks a call
            pass

    # ---- audit
    def audit(self, ident: Optional[Identity], tool: str, args=(), ok: bool = True, error: str = "", ms: int = 0, items: int = 0,
              direction: str = "in", principal: str = "", scope: str = "", team_id: Optional[int] = None) -> None:
        """One row per call. `args` are argument NAMES only (never values: they can carry personal data)."""
        try:
            names = ",".join(sorted(str(a)[:40] for a in (args or ())))[:300]
            self.kb._exec("""INSERT INTO mcp_audit (ts, direction, key_id, key_name, principal, scope, tool, args, ok, error, ms, items, team_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (_now(), direction, ident.key_id if ident else None, (ident.name if ident else "")[:80],
                                                       (principal or (ident.principal if ident else ""))[:200], (scope or (ident.scope if ident else ""))[:200],
                                                       (tool or "")[:120], names, int(bool(ok)), (error or "")[:200], int(ms), int(items),
                                                       team_id if team_id is not None else (ident.team_id if ident else None)))
            with _LOCK:
                self._n += 1
                prune = self._n % 500 == 1
            if prune:
                cut = (datetime.now(UTC) - timedelta(days=AUDIT_DAYS)).isoformat(timespec="seconds")
                self.kb._exec("DELETE FROM mcp_audit WHERE ts < ?", (cut,))
        except Exception:  # noqa: BLE001 - the audit never breaks the call it records
            pass

    def audit_list(self, n: int = 200, key_id: Optional[int] = None, direction: Optional[str] = None) -> list:
        q, p = "SELECT * FROM mcp_audit WHERE 1=1", []
        if key_id is not None:
            q += " AND key_id=?"; p.append(key_id)
        if direction:
            q += " AND direction=?"; p.append(direction)
        q += " ORDER BY id DESC LIMIT ?"; p.append(max(1, min(int(n), 2000)))
        return [dict(r) for r in self.kb._exec(q, tuple(p))]


def store(svc) -> McpKeys:
    """The installation's key store (created on first use, kept on the Services object)."""
    s = getattr(svc, "mcp_keys", None)
    if s is None:
        with _LOCK:
            s = getattr(svc, "mcp_keys", None)
            if s is None:
                s = McpKeys(svc.kb)
                svc.mcp_keys = s
    return s


def presented(headers) -> str:
    """The key a client sent: Authorization: Bearer <key> or X-API-Key."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    given = h.get("x-api-key") or ""
    if not given:
        auth = h.get("authorization", "")
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
    return given.strip()


def identify(svc, given: str) -> Optional[Identity]:
    """A presented key -> Identity, or None (unknown, revoked, or bound to a disabled account / deleted team)."""
    given = (given or "").strip()
    if not given:
        return None
    env = os.environ.get("MCP_API_KEY", "")
    if env and secrets.compare_digest(given.encode("utf-8"), env.encode("utf-8")):
        return install_identity(os.environ.get("MCP_API_KEY_WRITE", "") == "1")
    row = store(svc).verify(given)
    if not row:
        return None
    write = bool(row.get("can_write"))
    if row["kind"] == "team":
        try:
            team = svc.teams.get(int(row["team_id"]))
        except (KeyError, TypeError, ValueError):
            return None
        ident = team_identity(team, row["id"], row["name"], write)
    else:
        u = svc.users.get(row.get("email") or "")
        if not u or u.get("status") != "active":
            return None
        full = svc.access_of({"email": u["email"], "role": u.get("role", "")})
        acc = restrict(full, READ_PERMS | (WRITE_PERMS if write else frozenset()))
        ident = Identity(row["id"], row["name"], u["email"], "all" if full.wide else f"user:{u['email']}", acc, write)
    store(svc).touch(row["id"])
    return ident
