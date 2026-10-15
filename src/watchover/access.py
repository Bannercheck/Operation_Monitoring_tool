"""Effective permissions of one account: the installation role plus the team role of every membership.

    G      = permissions of the installation role (users.role; admin holds everything)
    R_T    = permissions of the user's team role in team T (only team-assignable keys)
    H(T)   = hosts of team T (rules + explicit hosts)

    perms        = G ∪ ⋃ R_T
    scope(p)     = None (everything) for admin / scope.all; for a user in no team None (or nothing when teams_strict);
                   else ⋃ H(T) over every membership when p ∈ G, or over the memberships whose role holds p
    can_in(T, p) = p ∈ G or p ∈ R_T (installation-wide users: p ∈ perms)
    install(p)   = p ∈ G and the user is installation-wide (admin, scope.all, or no team and not strict)
    manages(T)   = page.teams ∈ G or team.manage ∈ R_T

Pure logic (no FastAPI); `AccessResolver` caches one Access per account for 30 s and drops it as soon as a role or a team
changes (version counters on Roles and Teams), so edits apply on the next request.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .rbac import ALL, TEAM_ONLY, normalize
from .teams import Scope


@dataclass(frozen=True)
class Membership:
    team_id: int
    team: str
    role_id: Optional[int]
    role_key: str
    role_label: str
    perms: frozenset
    hosts: frozenset
    service_rules: tuple


@dataclass
class Access:
    email: str
    role: str
    all: bool
    strict: bool
    global_perms: frozenset
    memberships: tuple
    perms: frozenset = frozenset()
    _scopes: dict = field(default_factory=dict, repr=False)
    owners_fn: object = field(default=None, repr=False, compare=False)    # teams.owners(host) -> [team ids] (can_approve)

    def __post_init__(self):
        if not self.perms:
            p = set(self.global_perms)
            for m in self.memberships:
                p |= m.perms
            self.perms = frozenset(ALL) if self.role == "admin" else frozenset(p)

    # ---- checks
    def can(self, perm: str) -> bool:
        return perm in self.perms

    @property
    def wide(self) -> bool:
        """Installation-wide view: admin, scope.all, or a user in no team while teams_strict is off."""
        return self.all or (not self.memberships and not self.strict)

    def membership(self, team_id) -> Optional[Membership]:
        return next((m for m in self.memberships if m.team_id == team_id), None)

    def team_ids(self, perm: Optional[str] = None) -> list:
        """The user's teams in which `perm` applies (every membership for perm=None)."""
        if perm is None or perm in self.global_perms or self.all:
            return [m.team_id for m in self.memberships] if (perm is None or perm in self.perms) else []
        return [m.team_id for m in self.memberships if perm in m.perms]

    def can_in(self, team_id, perm: str) -> bool:
        if self.all:
            return perm in self.perms
        m = self.membership(team_id)
        return m is not None and (perm in self.global_perms or perm in m.perms)

    def install(self, perm: str) -> bool:
        return perm in self.global_perms and self.wide

    def can_approve(self, host: str, perm: str = "act.approve") -> bool:
        """May this account approve work on `host` (action agent votes, §10.9)? Unlike can_in there is no `all` bypass: the
        user must hold `perm` in a team that owns the host; an installation-wide host needs `perm` installation-wide; an
        installation-wide permission covers a team-owned host only for a `wide` user who also holds act.policy."""
        owners = []
        if host and self.owners_fn is not None:
            try:
                owners = list(self.owners_fn(host))
            except Exception:  # noqa: BLE001 - unknown ownership fails closed to the installation rule
                owners = []
        if owners:
            for m in self.memberships:
                if m.team_id in owners and (perm in m.perms or perm in self.global_perms):
                    return True
            return perm in self.global_perms and self.wide and "act.policy" in self.global_perms
        return perm in self.global_perms and self.wide

    def manages(self, team_id) -> bool:
        if "page.teams" in self.global_perms:
            return True
        m = self.membership(team_id)
        return m is not None and "team.manage" in m.perms

    def managed(self) -> list:
        return [m.team_id for m in self.memberships if "team.manage" in m.perms]

    # ---- data scope
    def scope(self, perm: Optional[str] = None) -> Optional[Scope]:
        """None: every host. A Scope: the hosts / services of the teams in which `perm` applies (None: every membership)."""
        if self.all:
            return None
        if not self.memberships:
            return Scope(set(), [], []) if self.strict else None
        if perm in self._scopes:
            return self._scopes[perm]
        if perm is None or perm in self.global_perms:
            ms = list(self.memberships)
        else:
            ms = [m for m in self.memberships if perm in m.perms]
        hosts: set = set()
        rules: list = []
        for m in ms:
            hosts |= set(m.hosts)
            rules += list(m.service_rules)
        sc = Scope(hosts, rules, [m.team for m in ms])
        self._scopes[perm] = sc
        return sc

    def visible_team(self, team_id, perm: str) -> bool:
        """May the user read a team-owned record (team_id None = installation-wide) with `perm`?"""
        if self.wide:
            return True
        return team_id is not None and team_id in self.team_ids(perm)

    def summary(self) -> dict:
        """The /auth/me additions."""
        return {"all": self.wide, "global_permissions": sorted(self.global_perms),
                "teams": [{"id": m.team_id, "name": m.team, "role_id": m.role_id, "role_label": m.role_label, "role_key": m.role_key,
                           "permissions": sorted(m.perms), "hosts": len(m.hosts), "manage": "team.manage" in m.perms} for m in self.memberships]}


class AccessResolver:
    TTL = 30.0

    def __init__(self, roles, teams, settings_fn):
        self.roles, self.teams, self.settings_fn = roles, teams, settings_fn
        self._cache: dict = {}
        self.lock = threading.Lock()

    def invalidate(self) -> None:
        self._cache.clear()

    def of(self, user: dict) -> Access:
        email = (user.get("email") or "").lower()
        role = user.get("role") or ""
        try:
            strict = bool((self.settings_fn() or {}).get("teams_strict"))
        except Exception:  # noqa: BLE001
            strict = False
        key = (role, getattr(self.teams, "version", 0), getattr(self.roles, "version", 0), strict)
        hit = self._cache.get(email)
        now = time.time()
        if hit and hit[0] == key and now - hit[1] < self.TTL:
            return hit[2]
        acc = self._build(email, role, strict)
        with self.lock:
            self._cache[email] = (key, now, acc)
        return acc

    def _build(self, email: str, role: str, strict: bool) -> Access:
        g = frozenset(ALL) if role == "admin" else frozenset(set(self.roles.perms(role)) - TEAM_ONLY)
        all_ = role == "admin" or "scope.all" in g
        ms = []
        for t, r in self.teams.memberships_of(email):
            perms = frozenset(normalize(r["perms"], team=True)) if r else frozenset()
            ms.append(Membership(team_id=t["id"], team=t["name"], role_id=(r or {}).get("id"), role_key=(r or {}).get("key", ""), role_label=(r or {}).get("label", ""),
                                 perms=perms, hosts=frozenset(t["matched"]), service_rules=tuple(x for x in t["rules"] if x["field"] == "service")))
        return Access(email=email, role=role, all=all_, strict=strict, global_perms=g, memberships=tuple(ms),
                      owners_fn=getattr(self.teams, "owners", None))


def wire(svc) -> None:
    """Called once by Services: the resolver, and the team hooks of agents (team enrolment keys), thresholds and alert rules
    (a team's policy / rule covers only the team's hosts) and of team deletion (datasets / actions owned by the team)."""
    from . import settings as wo_settings
    svc.access = AccessResolver(svc.roles, svc.teams, wo_settings.load)
    svc.teams.roles_store = svc.roles
    try:
        if svc.kb._exec("SELECT 1 FROM teams LIMIT 1"):
            svc.roles.ensure_member()
    except Exception:  # noqa: BLE001
        pass

    def team_hosts(tid):
        try:
            return set(svc.teams.get(int(tid))["matched"])
        except Exception:  # noqa: BLE001 - a deleted team covers nothing
            return set()
    for name in ("agents", "thresholds", "alerts"):
        obj = getattr(svc, name, None)
        if obj is not None:
            obj.team_hooks = svc.teams
            obj.team_hosts_fn = team_hosts
    try:
        from .ownership import attach_owned_hooks
        attach_owned_hooks(svc)
    except Exception:  # noqa: BLE001
        pass
    from .ingest_guard import wire as wire_guard          # team agents / sources post only for their team's hosts (Python and Go paths)
    wire_guard(svc)
