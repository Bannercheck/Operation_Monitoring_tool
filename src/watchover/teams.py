"""Teams and host scope: several teams share one Watchover, each sees only its own servers.

A team owns hosts by rules (any rule matching is enough) and by explicit assignment:

    {"field": "host",        "op": "glob",   "value": "ea-*"}            host name pattern
    {"field": "env",         "op": "equals", "value": "prod"}            environment (registered agent, inventory or logs)
    {"field": "application", "op": "glob",   "value": "SAP*"}            inventory application (also role / owner / dc / cluster)
    {"field": "site" | "tag","op": "equals", "value": "ist"}             agent site / tag given at enrolment
    {"field": "ip",          "op": "cidr",   "value": "10.20.0.0/16"}    inventory IP inside a network
    {"field": "service",     "op": "glob",   "value": "hr-*"}            a service / mobile or web app by name (APM, user experience)

Who sees what: administrators and roles holding `scope.all` see everything; a user who belongs to one or more teams sees the
union of those teams' hosts and services; a user in no team sees everything (so adding teams never locks existing users out),
unless System › teams_strict is on, in which case a user without a team sees nothing host-scoped.

Role-based teams (v1.46): every team carries its own roles (`team_roles`; three from templates on creation: Ekip yöneticisi,
Operatör, İzleyici) and every membership one team role. A permission from a team role applies only on that team's hosts; the
installation role (users.role) applies on all of the user's teams (see access.py). Explicit hosts are the team's owned inventory:
`claim()` is the single gate that decides whether a host may join a team (its own / brand-new hosts yes, another team's no).
"""
from __future__ import annotations

import fnmatch
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

UTC = timezone.utc
FIELDS = ("host", "env", "application", "role", "owner", "dc", "cluster", "site", "tag", "ip", "service")
OPS = ("equals", "glob", "prefix", "cidr")
NONE = ("\x00no-host-in-scope",)                     # a host filter that matches nothing
ORIGINS = ("manual", "inventory", "promote", "agent", "import", "application")
OWNED_TABLES = {"thresholds": "threshold_policies", "recipients": "recipients", "groups": "notify_groups", "rules": "notify_rules", "agents": "agents",
                "sources": "sources", "flows": "biz_flows"}     # team-owned rows (team_id); datasets / actions are added by the services hook


class OwnedError(ValueError):
    """A team still owns records: the caller must transfer them (to the installation or another team) before deleting."""
    def __init__(self, owned: dict):
        super().__init__("the team owns records: transfer them first")
        self.owned = owned


def _slug(label: str) -> str:
    tr = str.maketrans("çğıöşüÇĞİÖŞÜâî", "cgiosuCGIOSUai")
    s_ = re.sub(r"[^a-z0-9]+", "_", (label or "").translate(tr).lower()).strip("_")
    return s_[:40] or "role"



def _match(op: str, pattern: str, value: str) -> bool:
    if value is None:
        return False
    v, p = str(value).strip().lower(), str(pattern).strip().lower()
    if not p:
        return False
    if op == "equals":
        return v == p
    if op == "prefix":
        return v.startswith(p)
    if op == "cidr":
        try:
            return ipaddress.ip_address(v) in ipaddress.ip_network(p, strict=False)
        except ValueError:
            return False
    return fnmatch.fnmatchcase(v, p)


class Scope:
    """What one user may see. `hosts` is the concrete set of allowed host names (known hosts filtered by the rules)."""
    def __init__(self, hosts: set[str], service_rules: list[dict], teams: list[str]):
        self.hosts = hosts
        self.service_rules = service_rules
        self.teams = teams
        self._svc_hosts: dict[str, set[str]] = {}

    def allows(self, host: str | None) -> bool:
        return bool(host) and host in self.hosts

    def allows_service(self, name: str, hosts_of_service: set[str] | None = None) -> bool:
        if any(_match(r.get("op", "glob"), r.get("value", ""), name) for r in self.service_rules):
            return True
        return bool(hosts_of_service and hosts_of_service & self.hosts)

    def host_filter(self, requested=None):
        """For the live store's host argument (a name or a tuple): the requested host(s) narrowed to the scope."""
        if requested is None:
            return tuple(sorted(self.hosts)) or NONE
        req = (requested,) if isinstance(requested, str) else tuple(requested)
        keep = tuple(h for h in req if h in self.hosts)
        return (keep[0] if len(keep) == 1 else keep) if keep else NONE


class Teams:
    def __init__(self, kb, services=None):
        self.kb, self.svc = kb, services
        self.lock = threading.Lock()
        self.version = 0                                     # bumped by every change: the access cache drops entries that predate it
        self.roles_store = None                              # the installation Roles (set by access.wire): member role, migration mapping
        self.owned_hooks: dict = {}                          # name -> (count(tid) -> int, transfer(tid, to_tid | None) -> None), e.g. datasets
        pk = kb.pk
        kb._exec(f"CREATE TABLE IF NOT EXISTS teams (id {pk}, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', rules TEXT DEFAULT '[]', created_at TEXT)")
        kb._exec("CREATE TABLE IF NOT EXISTS team_hosts (team_id INTEGER NOT NULL, host TEXT NOT NULL, PRIMARY KEY (team_id, host))")
        kb._exec("CREATE TABLE IF NOT EXISTS team_members (team_id INTEGER NOT NULL, email TEXT NOT NULL, PRIMARY KEY (team_id, email))")
        kb._exec(f"""CREATE TABLE IF NOT EXISTS team_roles (id {pk}, team_id INTEGER NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL, description TEXT DEFAULT '',
            perms TEXT DEFAULT '[]', template TEXT DEFAULT '', position INTEGER DEFAULT 0, updated_at TEXT, UNIQUE (team_id, key))""")
        kb._exec("CREATE TABLE IF NOT EXISTS team_enroll_keys (team_id INTEGER PRIMARY KEY, key TEXT NOT NULL, created_at TEXT)")
        for table, col, ddl in (("team_members", "role_id", "INTEGER"), ("teams", "default_role_id", "INTEGER"), ("team_hosts", "origin", "TEXT DEFAULT 'manual'"),
                                ("team_hosts", "added_by", "INTEGER"), ("team_hosts", "added_at", "TEXT DEFAULT ''")):
            try:
                if col not in kb.columns(table):
                    kb._exec(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except Exception:  # noqa: BLE001 - a concurrent start already added it
                pass
        self._facts: tuple[float, dict] | None = None
        self._scopes: dict[str, tuple[float, Scope | None]] = {}
        self._matched: dict[int, tuple] = {}
        self._list: tuple[int, float, list] | None = None
        self.migrate()

    # ---------------------------------------------------------------- migration (idempotent, every start)
    def migrate(self) -> None:
        """team_roles_v1: a team without roles gets the three template roles; a member without a team role is mapped from the
        installation role (admin -> manager, operator -> operator, anything else -> viewer). Nobody loses access: the installation
        role still applies in every team of the user."""
        from .rbac import TEMPLATE_ORDER  # noqa: F401 - import check only
        teams = self.kb._exec("SELECT id FROM teams")
        changed = False
        for t in teams:
            tid = t["id"]
            if not self.kb._exec("SELECT 1 FROM team_roles WHERE team_id=? LIMIT 1", (tid,)):
                self._create_templates(tid); changed = True
            elif not (self.kb._exec("SELECT default_role_id FROM teams WHERE id=?", (tid,))[0].get("default_role_id")):
                self._fix_default(tid); changed = True
            missing = self.kb._exec("SELECT email FROM team_members WHERE team_id=? AND role_id IS NULL", (tid,))
            if missing:
                by_key = {r["key"]: r["id"] for r in self.kb._exec("SELECT id, key FROM team_roles WHERE team_id=?", (tid,))}
                for m in missing:
                    role = self._install_role(m["email"])
                    key = "manager" if role == "admin" else "operator" if role == "operator" else "viewer"
                    rid = by_key.get(key) or self._default_role(tid)
                    self.kb._exec("UPDATE team_members SET role_id=? WHERE team_id=? AND email=?", (rid, tid, m["email"]))
                changed = True
        if changed:
            self.invalidate()

    def _install_role(self, email: str) -> str:
        try:
            rows = self.kb._exec("SELECT role FROM users WHERE lower(email)=?", (email.lower(),))
            return rows[0]["role"] if rows else ""
        except Exception:  # noqa: BLE001 - no users table (engine used without accounts)
            return ""

    # ---------------------------------------------------------------- CRUD
    def list(self) -> list[dict]:
        """Every team with rules, explicit hosts (+ origin), members (+ team role), roles and the hosts its rules match now. Cached
        until the next change or 30 s (rules are re-evaluated against new hosts)."""
        hit = self._list
        if hit and hit[0] == self.version and time.time() - hit[1] < 30:
            return [dict(t) for t in hit[2]]
        q = self.kb._exec
        hosts: dict[int, list[dict]] = {}
        for x in q("SELECT team_id, host, origin, added_by, added_at FROM team_hosts ORDER BY host"):
            hosts.setdefault(x["team_id"], []).append({"host": x["host"], "origin": x.get("origin") or "manual", "added_by": x.get("added_by"), "added_at": x.get("added_at") or ""})
        members: dict[int, list[dict]] = {}
        for x in q("SELECT team_id, email, role_id FROM team_members ORDER BY email"):
            members.setdefault(x["team_id"], []).append({"email": x["email"], "role_id": x.get("role_id")})
        roles: dict[int, list[dict]] = {}
        for x in q("SELECT * FROM team_roles ORDER BY position, id"):
            roles.setdefault(x["team_id"], []).append(self._role_row(x))
        out = []
        for r in q("SELECT * FROM teams ORDER BY name"):
            d = dict(r); d["rules"] = json.loads(d.get("rules") or "[]")
            d["host_meta"] = hosts.get(d["id"], [])
            d["hosts"] = [h["host"] for h in d["host_meta"]]
            d["memberships"] = members.get(d["id"], [])
            d["members"] = [m["email"] for m in d["memberships"]]
            d["roles"] = roles.get(d["id"], [])
            d["matched"] = sorted(self.team_hosts(d))
            out.append(d)
        self._list = (self.version, time.time(), out)
        return [dict(t) for t in out]

    def get(self, tid: int) -> dict:
        for t in self.list():
            if t["id"] == tid:
                return t
        raise KeyError(tid)

    @staticmethod
    def _clean_rules(rules: list) -> list[dict]:
        out = []
        for r in rules or []:
            f, op, v = str(r.get("field", "")).lower(), str(r.get("op", "glob")).lower(), str(r.get("value", "")).strip()
            if f not in FIELDS or op not in OPS or not v:
                raise ValueError(f"rule needs field in {FIELDS}, op in {OPS} and a value")
            if op == "cidr":
                ipaddress.ip_network(v, strict=False)
            row = {"field": f, "op": op, "value": v[:200]}
            if r.get("origin") in ("manual", "rum"):
                row["origin"] = r["origin"]
            out.append(row)
        return out

    def save(self, name: str, description: str = "", rules: list | None = None, hosts: list | None = None, members: list | None = None, tid: int | None = None,
             copy_roles_from: int | None = None, by: int | None = None) -> dict:
        """Create (with three template roles, or a copy of another team's roles) or update a team. `members`: e-mails (an existing
        member keeps their team role, a new one gets the default role) or {"email", "role" (key) | "role_id"}."""
        name = name.strip()[:80]
        if not name:
            raise ValueError("team name required")
        rules = self._clean_rules(rules or [])
        with self.lock:
            if tid is None:
                if self.kb._exec("SELECT id FROM teams WHERE name=?", (name,)):
                    raise ValueError(f"a team named '{name}' exists")
                tid = self.kb._insert("INSERT INTO teams (name, description, rules, created_at) VALUES (?,?,?,?)",
                                      (name, description[:300], json.dumps(rules), datetime.now(UTC).isoformat(timespec="seconds")))
                if copy_roles_from:
                    self._copy_roles(int(copy_roles_from), tid)
                else:
                    self._create_templates(tid)
            else:
                dup = self.kb._exec("SELECT id FROM teams WHERE name=? AND id<>?", (name, tid))
                if dup:
                    raise ValueError(f"a team named '{name}' exists")
                self.kb._exec("UPDATE teams SET name=?, description=?, rules=? WHERE id=?", (name, description[:300], json.dumps(rules), tid))
            if hosts is not None:
                want = [h.strip() for h in dict.fromkeys(hosts) if h and h.strip()]
                have = {x["host"] for x in self.kb._exec("SELECT host FROM team_hosts WHERE team_id=?", (tid,))}
                for h in have - set(want):
                    self.kb._exec("DELETE FROM team_hosts WHERE team_id=? AND host=?", (tid, h))
                now = datetime.now(UTC).isoformat(timespec="seconds")
                self.kb.executemany("INSERT INTO team_hosts (team_id, host, origin, added_by, added_at) VALUES (?,?,?,?,?)",
                                    [(tid, h, "manual", by, now) for h in want if h not in have])
            if members is not None:
                self._set_members(tid, members)
        if self.roles_store is not None:
            self.roles_store.ensure_member()
        self.invalidate()
        return self.get(tid)

    def _set_members(self, tid: int, members: list) -> None:
        roles = {r["id"]: r for r in self.kb._exec("SELECT id, key FROM team_roles WHERE team_id=?", (tid,))}
        by_key = {r["key"]: rid for rid, r in roles.items()}
        cur = {x["email"]: x.get("role_id") for x in self.kb._exec("SELECT email, role_id FROM team_members WHERE team_id=?", (tid,))}
        want: dict[str, int | None] = {}
        for m in members:
            if isinstance(m, dict):
                email = str(m.get("email") or "").strip().lower()
                rid = m.get("role_id") if m.get("role_id") in roles else by_key.get(str(m.get("role") or "")) if m.get("role") else None
            else:
                email, rid = str(m or "").strip().lower(), None
            if email:
                want[email] = rid or cur.get(email) or self._default_role(tid)
        for e in set(cur) - set(want):
            self.kb._exec("DELETE FROM team_members WHERE team_id=? AND email=?", (tid, e))
        for e, rid in want.items():
            if e in cur:
                if cur[e] != rid:
                    self.kb._exec("UPDATE team_members SET role_id=? WHERE team_id=? AND email=?", (rid, tid, e))
            else:
                self.kb._exec("INSERT INTO team_members (team_id, email, role_id) VALUES (?,?,?)", (tid, e, rid))

    def owned(self, tid: int) -> dict:
        """Team-owned records by kind (threshold policies, alert recipients / groups / rules, agents, sources, flows, + hooks)."""
        out = {}
        for k, table in OWNED_TABLES.items():
            try:
                if "team_id" in self.kb.columns(table):
                    out[k] = int(self.kb._exec(f"SELECT COUNT(*) AS n FROM {table} WHERE team_id=?", (tid,))[0]["n"])
                else:
                    out[k] = 0
            except Exception:  # noqa: BLE001 - table not created in this installation
                out[k] = 0
        for k, (count, _move) in self.owned_hooks.items():
            try:
                out[k] = int(count(tid))
            except Exception:  # noqa: BLE001
                out[k] = 0
        return out

    def delete(self, tid: int, transfer: str | None = None) -> None:
        """Delete a team. When it owns records, `transfer` must say where they go: "install" (installation-wide; policies and
        alert rules are disabled so they do not suddenly cover every host) or another team's id."""
        owned = {k: v for k, v in self.owned(tid).items() if v}
        if owned:
            if not transfer:
                raise OwnedError(owned)
            to = None if transfer == "install" else int(transfer)
            if to is not None:
                self.get(to)
            for k, table in OWNED_TABLES.items():
                if owned.get(k):
                    extra = ", enabled=0" if to is None and k in ("thresholds", "rules") else ""
                    self.kb._exec(f"UPDATE {table} SET team_id=?{extra} WHERE team_id=?", (to, tid))
            for k, (_count, move) in self.owned_hooks.items():
                if owned.get(k):
                    move(tid, to)
        for t in ("team_hosts", "team_members", "team_roles", "team_enroll_keys"):
            self.kb._exec(f"DELETE FROM {t} WHERE team_id=?", (tid,))
        self.kb._exec("DELETE FROM teams WHERE id=?", (tid,))
        self.invalidate()

    def invalidate(self) -> None:
        self._scopes.clear(); self._facts = None; self._matched.clear(); self._list = None
        self.version += 1

    def teams_of(self, email: str) -> list[dict]:
        ids = {r["team_id"] for r in self.kb._exec("SELECT team_id FROM team_members WHERE email=?", (email.lower(),))}
        return [t for t in self.list() if t["id"] in ids]

    # ---------------------------------------------------------------- team roles
    @staticmethod
    def _role_row(x: dict) -> dict:
        from .rbac import normalize
        d = dict(x)
        d["perms"] = sorted(normalize(json.loads(d.get("perms") or "[]"), team=True))
        return d

    def roles(self, tid: int) -> list[dict]:
        return [self._role_row(x) for x in self.kb._exec("SELECT * FROM team_roles WHERE team_id=? ORDER BY position, id", (tid,))]

    def role(self, tid: int, rid: int) -> dict:
        rows = self.kb._exec("SELECT * FROM team_roles WHERE team_id=? AND id=?", (tid, rid))
        if not rows:
            raise KeyError(f"role {rid}")
        return self._role_row(rows[0])

    def _create_templates(self, tid: int) -> None:
        from .rbac import TEAM_TEMPLATES, TEMPLATE_ORDER
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for pos, key in enumerate(TEMPLATE_ORDER):
            t = TEAM_TEMPLATES[key]
            self.kb._exec("INSERT INTO team_roles (team_id, key, label, description, perms, template, position, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (tid, key, t["label"][0], t["desc"][0], json.dumps(sorted(t["perms"])), key, pos, now))
        self._fix_default(tid)

    def _copy_roles(self, src: int, tid: int) -> None:
        rows = self.kb._exec("SELECT * FROM team_roles WHERE team_id=? ORDER BY position, id", (src,))
        if not rows:
            raise KeyError(f"team {src}")
        src_default = (self.kb._exec("SELECT default_role_id FROM teams WHERE id=?", (src,)) or [{}])[0].get("default_role_id")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        default_key = None
        for r in rows:
            self.kb._exec("INSERT INTO team_roles (team_id, key, label, description, perms, template, position, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (tid, r["key"], r["label"], r.get("description") or "", r.get("perms") or "[]", r.get("template") or "", r.get("position") or 0, now))
            if r["id"] == src_default:
                default_key = r["key"]
        if default_key:
            rid = self.kb._exec("SELECT id FROM team_roles WHERE team_id=? AND key=?", (tid, default_key))[0]["id"]
            self.kb._exec("UPDATE teams SET default_role_id=? WHERE id=?", (rid, tid))
        else:
            self._fix_default(tid)

    def _fix_default(self, tid: int) -> None:
        """Default role: the viewer template, else the role with the fewest permissions."""
        rows = self.roles(tid)
        if not rows:
            return
        pick = next((r for r in rows if r.get("template") == "viewer"), None) or min(rows, key=lambda r: (len(r["perms"]), r["id"]))
        self.kb._exec("UPDATE teams SET default_role_id=? WHERE id=?", (pick["id"], tid))

    def _default_role(self, tid: int) -> int | None:
        rows = self.kb._exec("SELECT default_role_id FROM teams WHERE id=?", (tid,))
        rid = rows[0].get("default_role_id") if rows else None
        if not rid:
            self._fix_default(tid)
            rows = self.kb._exec("SELECT default_role_id FROM teams WHERE id=?", (tid,))
            rid = rows[0].get("default_role_id") if rows else None
        return rid

    def create_role(self, tid: int, label: str, description: str = "", perms=(), template: str = "") -> dict:
        from .rbac import normalize
        label = (label or "").strip()[:60]
        if not label:
            raise ValueError("role label required")
        self.get(tid)
        keys = {r["key"] for r in self.kb._exec("SELECT key FROM team_roles WHERE team_id=?", (tid,))}
        base = _slug(label); key = base; n = 2
        while key in keys:
            key = f"{base}_{n}"; n += 1
        pos = len(keys)
        rid = self.kb._insert("INSERT INTO team_roles (team_id, key, label, description, perms, template, position, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                              (tid, key, label, (description or "")[:300], json.dumps(sorted(normalize(perms, team=True))), template or "", pos,
                               datetime.now(UTC).isoformat(timespec="seconds")))
        self.invalidate()
        return self.role(tid, rid)

    def update_role(self, tid: int, rid: int, label: str | None = None, description: str | None = None, perms=None) -> dict:
        from .rbac import normalize
        cur = self.role(tid, rid)
        self.kb._exec("UPDATE team_roles SET label=?, description=?, perms=?, updated_at=? WHERE id=?",
                      ((label or "").strip()[:60] or cur["label"], cur.get("description") or "" if description is None else description[:300],
                       json.dumps(sorted(normalize(cur["perms"] if perms is None else perms, team=True))), datetime.now(UTC).isoformat(timespec="seconds"), rid))
        self.invalidate()
        return self.role(tid, rid)

    def reset_role(self, tid: int, rid: int) -> dict:
        from .rbac import TEAM_TEMPLATES
        cur = self.role(tid, rid)
        t = TEAM_TEMPLATES.get(cur.get("template") or "")
        if not t:
            raise ValueError("this role was not made from a template")
        return self.update_role(tid, rid, perms=t["perms"])

    def delete_role(self, tid: int, rid: int, move_to: int | None = None) -> None:
        self.role(tid, rid)
        others = [r for r in self.roles(tid) if r["id"] != rid]
        if not others:
            raise ValueError("a team keeps at least one role")
        held = self.kb._exec("SELECT COUNT(*) AS n FROM team_members WHERE team_id=? AND role_id=?", (tid, rid))[0]["n"]
        if held:
            if not move_to or not any(r["id"] == move_to for r in others):
                raise RoleHeldError(int(held))
            self.kb._exec("UPDATE team_members SET role_id=? WHERE team_id=? AND role_id=?", (move_to, tid, rid))
        self.kb._exec("DELETE FROM team_roles WHERE id=?", (rid,))
        if self._default_role_raw(tid) == rid:
            self._fix_default(tid)
        self.invalidate()

    def _default_role_raw(self, tid: int):
        rows = self.kb._exec("SELECT default_role_id FROM teams WHERE id=?", (tid,))
        return rows[0].get("default_role_id") if rows else None

    def set_default_role(self, tid: int, rid: int) -> None:
        self.role(tid, rid)
        self.kb._exec("UPDATE teams SET default_role_id=? WHERE id=?", (rid, tid))
        self.invalidate()

    # ---------------------------------------------------------------- members
    def member_role(self, tid: int, email: str) -> int | None:
        rows = self.kb._exec("SELECT role_id FROM team_members WHERE team_id=? AND email=?", (tid, email.lower()))
        return rows[0].get("role_id") if rows else None

    def is_member(self, tid: int, email: str) -> bool:
        return bool(self.kb._exec("SELECT 1 FROM team_members WHERE team_id=? AND email=?", (tid, email.lower())))

    def set_member(self, tid: int, email: str, role_id: int | None = None) -> dict:
        email = email.strip().lower()
        rid = role_id or self._default_role(tid)
        self.role(tid, rid)
        if self.is_member(tid, email):
            self.kb._exec("UPDATE team_members SET role_id=? WHERE team_id=? AND email=?", (rid, tid, email))
        else:
            self.kb._exec("INSERT INTO team_members (team_id, email, role_id) VALUES (?,?,?)", (tid, email, rid))
        if self.roles_store is not None:
            self.roles_store.ensure_member()
        self.invalidate()
        return {"team_id": tid, "email": email, "role_id": rid}

    def remove_member(self, tid: int, email: str) -> None:
        self.kb._exec("DELETE FROM team_members WHERE team_id=? AND email=?", (tid, email.strip().lower()))
        self.invalidate()

    def memberships_of(self, email: str) -> list[tuple[dict, dict | None]]:
        """[(team, team role)] of one account (the role is None only for a member whose role was deleted underneath)."""
        rows = self.kb._exec("SELECT team_id, role_id FROM team_members WHERE email=?", ((email or "").lower(),))
        if not rows:
            return []
        teams = {t["id"]: t for t in self.list()}
        out = []
        for r in rows:
            t = teams.get(r["team_id"])
            if t is None:
                continue
            role = next((x for x in t["roles"] if x["id"] == r.get("role_id")), None)
            out.append((t, role))
        return sorted(out, key=lambda x: x[0]["name"])

    # ---------------------------------------------------------------- owned hosts
    def known(self, host: str) -> bool:
        if host in self.facts():
            return True
        try:
            return bool(self.kb._exec("SELECT 1 FROM inventory WHERE hostname=?", (host,)))
        except Exception:  # noqa: BLE001
            return False

    def claim(self, tid: int, hosts, origin: str = "manual", by: int | None = None, admin: bool = False, move: bool = False,
              self_claim: bool = False) -> tuple[list[str], list[dict]]:
        """The single gate for adding hosts to a team (explicit = owned). Accepted: a host the team already sees, a host Watchover
        does not know yet (pre-registration), an unowned host when the caller is an installation admin (or teams_self_claim is on).
        Rejected: a host another team sees ('other_team', never naming it) and a known host no team owns ('unowned')."""
        origin = origin if origin in ORIGINS else "manual"
        teams = self.list()
        me = next((t for t in teams if t["id"] == tid), None)
        if me is None:
            raise KeyError(tid)
        mine = set(me["matched"])
        added, rejected = [], []
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for h in dict.fromkeys(str(x or "").strip() for x in hosts or []):
            if not h:
                continue
            if h in me["hosts"]:
                added.append(h); continue
            # another team's host, or a brand-new name its host-name rule matches (it becomes theirs the moment it is seen)
            others = [t for t in teams if t["id"] != tid and (h in t["matched"] or self._host_rule_hit(t, h))]
            ok = h in mine
            if not ok and others:
                if admin and move:
                    for t in others:
                        self.kb._exec("DELETE FROM team_hosts WHERE team_id=? AND host=?", (t["id"], h))
                    still = [t for t in others if h in self.team_hosts({**t, "id": None, "hosts": [x for x in t["hosts"] if x != h]}) or self._host_rule_hit(t, h)]
                    if still:
                        rejected.append({"host": h, "reason": "other_team"}); continue
                    ok = True
                else:
                    rejected.append({"host": h, "reason": "other_team"}); continue
            if not ok:
                ok = (not self.known(h)) or admin or self_claim
                if not ok:
                    rejected.append({"host": h, "reason": "unowned"}); continue
            self.kb._exec("INSERT INTO team_hosts (team_id, host, origin, added_by, added_at) VALUES (?,?,?,?,?)", (tid, h, origin, by, now))
            added.append(h)
        if added:
            self.invalidate()
        return added, rejected

    def release(self, tid: int, host: str) -> None:
        if not self.kb._exec("SELECT 1 FROM team_hosts WHERE team_id=? AND host=?", (tid, host)):
            if host in self.get(tid)["matched"]:
                raise RuleMatchedError(host)
            raise KeyError(host)
        self.kb._exec("DELETE FROM team_hosts WHERE team_id=? AND host=?", (tid, host))
        self.invalidate()

    @staticmethod
    def _host_rule_hit(team: dict, host: str) -> bool:
        """A team's host-name rule matches this name (also before Watchover has seen the host)."""
        return any(r.get("field") == "host" and _match(r.get("op", "glob"), r.get("value", ""), host) for r in team.get("rules") or [])

    def owners(self, host: str) -> list[int]:
        """Ids of the teams that see a host (rules or explicit)."""
        return [t["id"] for t in self.list() if host in t["matched"]]

    def unowned(self) -> list[dict]:
        """Known hosts no team sees (installation admins assign them)."""
        seen = set()
        for t in self.list():
            seen |= set(t["matched"])
        return [{"host": h, "env": d.get("env") or "", "ip": d.get("ip") or "", "application": d.get("application") or ""}
                for h, d in sorted(self.facts().items()) if h not in seen]

    def conflicts(self) -> list[dict]:
        """Hosts held explicitly by more than one team (left by older versions; an installation admin resolves them)."""
        by: dict[str, list[str]] = {}
        for t in self.list():
            for h in t["hosts"]:
                by.setdefault(h, []).append(t["name"])
        return [{"host": h, "teams": names} for h, names in sorted(by.items()) if len(names) > 1]

    # ---------------------------------------------------------------- team enrolment keys (servers join the team on enrolment)
    def enroll_key(self, tid: int) -> str:
        rows = self.kb._exec("SELECT key FROM team_enroll_keys WHERE team_id=?", (tid,))
        return (rows[0]["key"] or "") if rows else ""

    def rotate_enroll_key(self, tid: int) -> str:
        self.get(tid)
        key = "wk_" + secrets.token_urlsafe(24)
        self.kb._exec("DELETE FROM team_enroll_keys WHERE team_id=?", (tid,))
        self.kb._exec("INSERT INTO team_enroll_keys (team_id, key, created_at) VALUES (?,?,?)", (tid, key, datetime.now(UTC).isoformat(timespec="seconds")))
        return key

    def disable_enroll_key(self, tid: int) -> None:
        self.kb._exec("DELETE FROM team_enroll_keys WHERE team_id=?", (tid,))

    def team_for_key(self, key: str) -> int | None:
        """The team whose enrolment key this is (constant-time compare against every key), else None."""
        if not key:
            return None
        hit = None
        for r in self.kb._exec("SELECT team_id, key FROM team_enroll_keys"):
            if r["key"] and hmac.compare_digest(str(r["key"]).encode(), key.encode()):
                hit = r["team_id"]
        return hit

    # ---------------------------------------------------------------- host facts and matching
    def facts(self) -> dict[str, dict]:
        """host -> {env, application, role, owner, dc, cluster, ip, site, tags}: every host Watchover knows (inventory, agents,
        live feed, rollups, flows), cached 30 s."""
        now = time.time()
        if self._facts and now - self._facts[0] < 30:
            return self._facts[1]
        f: dict[str, dict] = {}

        def add(h, **kw):
            if not h:
                return
            d = f.setdefault(h, {"tags": set()})
            for k, v in kw.items():
                if k == "tags":
                    d["tags"] |= {x.strip().lower() for x in str(v or "").split(",") if x.strip()}
                elif v and not d.get(k):
                    d[k] = v
        q = self.kb._exec
        try:
            for r in q("SELECT * FROM inventory"):
                add(r["hostname"], env=r.get("env"), application=r.get("application"), role=r.get("role"), owner=r.get("owner"), dc=r.get("dc"),
                    cluster=r.get("cluster"), ip=r.get("ip"))
        except Exception:  # noqa: BLE001
            pass
        try:
            for r in q("SELECT name, env, site, tags, last_ip FROM agents"):
                add(r["name"], env=r.get("env"), site=r.get("site"), tags=r.get("tags"), ip=r.get("last_ip"))
        except Exception:  # noqa: BLE001
            pass
        for sql in ("SELECT DISTINCT host, env FROM slo_minute", "SELECT DISTINCT host, env FROM net_flows"):
            try:
                for r in q(sql):
                    add(r["host"], env=r.get("env"))
            except Exception:  # noqa: BLE001
                pass
        if self.svc is not None:
            try:
                for h, e in self.svc.live.hosts().items():
                    add(h, env=e)
            except Exception:  # noqa: BLE001
                pass
        self._facts = (now, f)
        self._matched.clear()
        return f

    def team_hosts(self, team: dict) -> set[str]:
        facts = self.facts()
        rules = [r for r in team.get("rules", []) if r["field"] != "service"]
        key = (json.dumps(rules, sort_keys=True), tuple(sorted(team.get("hosts") or [])))
        tid = team.get("id")
        if tid is not None:
            hit = self._matched.get(tid)
            if hit and hit[0] == key:
                return set(hit[1])
        out = set(team.get("hosts") or [])
        for host, d in facts.items():
            for r in rules:
                fld = r["field"]
                if fld == "host":
                    vals = [host]
                elif fld == "tag":
                    vals = list(d.get("tags") or [])
                else:
                    vals = [d.get(fld)]
                if any(_match(r["op"], r["value"], v) for v in vals if v):
                    out.add(host); break
        if tid is not None:
            self._matched[tid] = (key, frozenset(out))
        return out

    # ---------------------------------------------------------------- scope of a user (legacy entry; access.py is the full model)
    def scope(self, user: dict, can_all: bool, strict: bool = False) -> Scope | None:
        """None: no restriction. A Scope: only these hosts / services."""
        if can_all:
            return None
        email = (user.get("email") or "").lower()
        hit = self._scopes.get(email)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        teams = self.teams_of(email)
        if not teams:
            sc = Scope(set(), [], []) if strict else None
        else:
            hosts: set[str] = set()
            svc_rules: list[dict] = []
            for t in teams:
                hosts |= set(t["matched"])
                svc_rules += [r for r in t["rules"] if r["field"] == "service"]
            sc = Scope(hosts, svc_rules, [t["name"] for t in teams])
        self._scopes[email] = (time.time(), sc)
        return sc


class RoleHeldError(ValueError):
    def __init__(self, n: int):
        super().__init__(f"the role is held by {n} member(s): pick a role to move them to")
        self.held = n


class RuleMatchedError(ValueError):
    def __init__(self, host: str):
        super().__init__(f"{host} is matched by a rule, not an explicit host: change the rules instead")
