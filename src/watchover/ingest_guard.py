"""Team senders post only for their own team's hosts (role-based teams, docs/design/team-roles.md 3.8).

An agent enrolled for a team (team enrolment key, or enrolled by a team account) and a pull source owned by a team are team
senders. LiveStore._accept asks this guard once per batch, so the Python receiver (`ingest`) and the Go parse path
(`ingest_parsed`, fed by the spool consumer) are covered by the same check. The team comes from the agent record (the
agent_id the receiver stamped after the token check; never from the request) or from the source row.

For every distinct host a batch names (after the "no host -> the sender's host" default):

    the team's host (its rules or explicit hosts)                       kept
    another team's host, or a brand-new name another team's host rule
    matches                                                             re-attributed to the sender's own host; the name
                                                                        the line gave stays in attribute host.reported
    a host no team owns: brand-new, or any when teams_self_claim is on  claimed for the team (Teams.claim, origin 'agent'),
                                                                        kept; at most CLAIMS_PER_MIN per sender per minute
    a known host no team owns                                           re-attributed (an installation admin assigns it)

The sender's own host follows the same rule; when even that is not the team's, lines are re-attributed to no host (only
installation-wide users see them). An agent without a team (fleet key, enrolled before role-based teams) whose own host
exactly one team owns counts as that team's sender, so existing tokens are covered without a migration. Installation-wide
senders (agents on hosts no team or several teams own, the legacy shared key, the simulator) are not limited. When the guard
itself fails, every foreign-looking host is re-attributed.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Iterable, Optional

from .teams import _match


class HostGuard:
    TTL = 10.0
    CLAIMS_PER_MIN = 30

    def __init__(self, teams, agents=None, settings_fn: Optional[Callable[[], dict]] = None):
        self.teams, self.agents, self.settings_fn = teams, agents, settings_fn
        self.lock = threading.Lock()
        self._agent_team: dict = {}                 # agent id -> (checked at, team id, agents.team_version)
        self._map: Optional[tuple] = None           # (teams.version, built at, host -> team ids, [(team id, host rules)], decisions)
        self._claims: dict = {}                     # (team, sender) -> deque of claim times
        self.reattributed = 0
        self.claimed = 0

    # ---------------------------------------------------------------- who is sending
    def team_of_agent(self, agent_id) -> Optional[int]:
        if agent_id is None or self.agents is None:
            return None
        now, v = time.time(), (getattr(self.agents, "team_version", 0), getattr(self.teams, "version", 0))
        hit = self._agent_team.get(agent_id)
        if hit and hit[2] == v and now - hit[0] < self.TTL:
            return hit[1]
        rec = self.agents.get(int(agent_id)) or {}
        tid = rec.get("team_id")
        if tid is None and rec.get("name"):                  # a fleet-key / pre-v1.46 agent on a host exactly one team owns is that team's
            owners = self._owner_ids(self._owners(), rec["name"])
            tid = next(iter(owners)) if len(owners) == 1 else None
        if len(self._agent_team) > 20000:
            self._agent_team.clear()
        self._agent_team[agent_id] = (now, tid, v)
        return tid

    # ---------------------------------------------------------------- who owns a host
    def _owners(self) -> tuple:
        v, now = getattr(self.teams, "version", 0), time.time()
        m = self._map
        if m and m[0] == v and now - m[1] < self.TTL:
            return m
        owners: dict = {}
        rules = []
        for t in self.teams.list():
            for h in t["matched"]:
                owners.setdefault(h, set()).add(t["id"])
            hr = [r for r in t["rules"] if r.get("field") == "host"]
            if hr:
                rules.append((t["id"], hr))
        m = (v, now, owners, rules, {})
        self._map = m
        return m

    @staticmethod
    def _owner_ids(m: tuple, host: str) -> set:
        ids = m[2].get(host)
        if ids is not None:
            return ids
        return {tid for tid, hr in m[3] if any(_match(r.get("op", "glob"), r.get("value", ""), host) for r in hr)}

    def _may_claim(self, team_id: int, sender: str) -> bool:
        q = self._claims.setdefault((team_id, sender), deque())
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.CLAIMS_PER_MIN:
            return False
        q.append(now)
        if len(self._claims) > 20000:
            self._claims.clear()
        return True

    def _decide(self, team_id: int, sender: str, hosts: Iterable[str]) -> set:
        """The hosts of `hosts` the team may post for (claiming unowned brand-new ones)."""
        m = self._owners()
        ok, claim = set(), []
        for h in hosts:
            if not h:
                continue
            seen = m[4].get((team_id, h))
            if seen is not None:
                if seen:
                    ok.add(h)
                continue
            ids = self._owner_ids(m, h)
            if team_id in ids:
                m[4][(team_id, h)] = True; ok.add(h)
            elif ids:
                m[4][(team_id, h)] = False
            else:
                claim.append(h)
        if claim:
            want = [h for h in claim if self._may_claim(team_id, sender)]
            added: list = []
            if want:
                try:
                    self_claim = bool((self.settings_fn() or {}).get("teams_self_claim")) if self.settings_fn else False
                except Exception:  # noqa: BLE001
                    self_claim = False
                added, _rej = self.teams.claim(team_id, want, "agent", self_claim=self_claim)
                self.claimed += len(added)
            for h in claim:
                if h in added:
                    ok.add(h)
                elif h in want:                     # refused by the claim rules: remember until the map is rebuilt
                    m[4][(team_id, h)] = False
        return ok

    # ---------------------------------------------------------------- the batch check
    def check(self, agent_id, team_id: Optional[int], sender: str, hosts: Iterable[str]) -> dict:
        """{host: replacement} for the hosts this sender may not post for; {} for an installation-wide sender."""
        hosts = {h for h in hosts if h}
        try:
            tid = team_id if team_id is not None else self.team_of_agent(agent_id)
            if tid is None:
                return {}
            with self.lock:
                ok = self._decide(int(tid), sender or "", hosts | ({sender} if sender else set()))
        except Exception:  # noqa: BLE001 - fail closed: everything but the sender's own name goes to the sender
            return {h: sender for h in hosts if h != sender}
        fallback = sender if sender in ok else ""
        out = {h: fallback for h in hosts if h not in ok}
        if out:
            self.reattributed += 1
        return out

    __call__ = check


def wire(svc) -> None:
    """Called by access.wire: the live store asks the guard for every batch of a team sender."""
    from . import settings as wo_settings
    svc.live.host_guard = HostGuard(svc.teams, getattr(svc, "agents", None), wo_settings.load)
