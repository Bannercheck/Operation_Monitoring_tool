"""Teams and their hosts: rules (name pattern, environment, inventory application / owner / role, agent site / tag, IP
network, service name) plus explicit hosts and members. A member sees only the team's hosts everywhere (live feed, anomalies,
APM, user experience, integrations, inventory, agents, maps, the assistant), unless the role holds scope.all."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...teams import FIELDS, OPS, Teams
from ..security import current_user, require, services

router = APIRouter(prefix="/teams", tags=["teams"])


class Rule(BaseModel):
    field: str
    op: str = "glob"
    value: str


class TeamIn(BaseModel):
    name: str
    description: str = ""
    rules: list[Rule] = []
    hosts: list[str] = []
    members: list[str] = []


@router.get("")
def list_teams(user=Depends(require("page.teams")), svc=Depends(services)):
    return {"teams": svc.teams.list(), "fields": FIELDS, "ops": OPS}


@router.get("/known-hosts")
def known_hosts(user=Depends(require("page.teams")), svc=Depends(services)):
    """Every host Watchover knows with the facts the rules can match (for building and checking rules)."""
    return [{"host": h, **{k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}} for h, d in sorted(svc.teams.facts().items())]


@router.post("/preview")
def preview(body: TeamIn, user=Depends(require("page.teams")), svc=Depends(services)):
    """Which hosts a draft team would own (before saving)."""
    rules = Teams._clean_rules([r.model_dump() for r in body.rules])
    return {"hosts": sorted(svc.teams.team_hosts({"rules": rules, "hosts": body.hosts}))}


@router.post("", status_code=201)
def create(body: TeamIn, user=Depends(require("page.teams")), svc=Depends(services)):
    return svc.teams.save(body.name, body.description, [r.model_dump() for r in body.rules], body.hosts, body.members)


@router.put("/{tid}")
def update(tid: int, body: TeamIn, user=Depends(require("page.teams")), svc=Depends(services)):
    svc.teams.get(tid)
    return svc.teams.save(body.name, body.description, [r.model_dump() for r in body.rules], body.hosts, body.members, tid=tid)


@router.delete("/{tid}", status_code=204)
def delete(tid: int, user=Depends(require("page.teams")), svc=Depends(services)):
    svc.teams.delete(tid)


@router.get("/mine")
def mine(user: dict = Depends(current_user), svc=Depends(services)):
    """The signed-in user's view: all hosts, or the teams and how many hosts they cover."""
    sc = svc.scope_of(user)
    return {"all": sc is None, "teams": sc.teams if sc else [t["name"] for t in svc.teams.teams_of(user["email"])], "hosts": len(sc.hosts) if sc else None}
