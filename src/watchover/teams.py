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
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import threading
import time
from datetime import datetime, timezone

UTC = timezone.utc
FIELDS = ("host", "env", "application", "role", "owner", "dc", "cluster", "site", "tag", "ip", "service")
OPS = ("equals", "glob", "prefix", "cidr")
NONE = ("\x00no-host-in-scope",)                     # a host filter that matches nothing


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
        pk = kb.pk
        kb._exec(f"CREATE TABLE IF NOT EXISTS teams (id {pk}, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', rules TEXT DEFAULT '[]', created_at TEXT)")
        kb._exec("CREATE TABLE IF NOT EXISTS team_hosts (team_id INTEGER NOT NULL, host TEXT NOT NULL, PRIMARY KEY (team_id, host))")
        kb._exec("CREATE TABLE IF NOT EXISTS team_members (team_id INTEGER NOT NULL, email TEXT NOT NULL, PRIMARY KEY (team_id, email))")
        self._facts: tuple[float, dict] | None = None
        self._scopes: dict[str, tuple[float, Scope | None]] = {}

    # ---------------------------------------------------------------- CRUD
    def list(self) -> list[dict]:
        out = []
        for r in self.kb._exec("SELECT * FROM teams ORDER BY name"):
            d = dict(r); d["rules"] = json.loads(d.get("rules") or "[]")
            d["hosts"] = [x["host"] for x in self.kb._exec("SELECT host FROM team_hosts WHERE team_id=? ORDER BY host", (d["id"],))]
            d["members"] = [x["email"] for x in self.kb._exec("SELECT email FROM team_members WHERE team_id=? ORDER BY email", (d["id"],))]
            d["matched"] = sorted(self.team_hosts(d))
            out.append(d)
        return out

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
            out.append({"field": f, "op": op, "value": v[:200]})
        return out

    def save(self, name: str, description: str = "", rules: list | None = None, hosts: list | None = None, members: list | None = None, tid: int | None = None) -> dict:
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
            else:
                self.kb._exec("UPDATE teams SET name=?, description=?, rules=? WHERE id=?", (name, description[:300], json.dumps(rules), tid))
            if hosts is not None:
                self.kb._exec("DELETE FROM team_hosts WHERE team_id=?", (tid,))
                self.kb.executemany("INSERT INTO team_hosts (team_id, host) VALUES (?,?)", [(tid, h.strip()) for h in dict.fromkeys(hosts) if h and h.strip()])
            if members is not None:
                self.kb._exec("DELETE FROM team_members WHERE team_id=?", (tid,))
                self.kb.executemany("INSERT INTO team_members (team_id, email) VALUES (?,?)", [(tid, m.strip().lower()) for m in dict.fromkeys(members) if m and m.strip()])
        self.invalidate()
        return self.get(tid)

    def delete(self, tid: int) -> None:
        for t in ("team_hosts", "team_members"):
            self.kb._exec(f"DELETE FROM {t} WHERE team_id=?", (tid,))
        self.kb._exec("DELETE FROM teams WHERE id=?", (tid,))
        self.invalidate()

    def invalidate(self) -> None:
        self._scopes.clear(); self._facts = None

    def teams_of(self, email: str) -> list[dict]:
        ids = {r["team_id"] for r in self.kb._exec("SELECT team_id FROM team_members WHERE email=?", (email.lower(),))}
        return [t for t in self.list() if t["id"] in ids]

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
        return f

    def team_hosts(self, team: dict) -> set[str]:
        facts = self.facts()
        rules = [r for r in team.get("rules", []) if r["field"] != "service"]
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
        return out

    # ---------------------------------------------------------------- scope of a user
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
