"""Agents (one token per server). A team-scoped account sees and changes only the agents of its teams' hosts; the fleet enrolment
key is installation-wide (teams have their own key under /api/teams/{tid}/enroll-key)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import access, claim_host, current_user, host_guard, perm_scope, require_install, services

router = APIRouter(prefix="/agents", tags=["agents"])


class EnrollIn(BaseModel):
    name: str
    env: str = ""
    site: str = ""
    tags: str = ""
    note: str = ""
    team_id: Optional[int] = None


class UpdateSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    pct: Optional[int] = None


class PinIn(BaseModel):
    version: str = ""


def _with_team(svc, rows: list) -> list:
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    upd = getattr(svc, "agent_updates", None)
    rel = (upd.release() or {}).get("version", "") if upd is not None else ""
    st = upd.settings() if upd is not None else None
    return [{**r, "team": names.get(r.get("team_id"), ""), **({"update": upd.state(r, rel, st)} if upd is not None else {})} for r in rows]


def _agent(svc, acc, aid: int, perm: str) -> dict:
    a = svc.agents.get(aid)
    if not a:
        raise KeyError(aid)
    host_guard(acc, a["name"], perm, "page.conn")
    return a


@router.get("")
def list_agents(svc=Depends(services), sc=Depends(perm_scope("page.conn"))):
    rows = svc.agents.list()
    return _with_team(svc, rows if sc is None else [r for r in rows if sc.allows(r["name"])])


@router.post("", status_code=201)
def enroll(body: EnrollIn, user=Depends(current_user), acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    """The plaintext token is returned once. A team-scoped account enrols agents for its team: the server name must be the team's
    host or a brand-new one (never another team's)."""
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    if any(r["name"].lower() == name.lower() and r["status"] == "active" for r in svc.agents.list()):
        raise ValueError(f"an active agent named '{name}' already exists: rotate its token instead of enrolling it twice")
    tid = claim_host(svc, acc, user, "act.connect", name, "agent", body.team_id)
    rec, token = svc.agents.enroll(name, body.env, body.site, body.tags, body.note)
    if tid is not None:
        svc.agents.set_team(rec["id"], tid)
        rec = svc.agents.get(rec["id"])
    return {"agent": rec, "token": token}


@router.get("/enroll-key")
def enroll_key(user=Depends(require_install("act.connect")), svc=Depends(services)):
    return {"key": svc.agents.enroll_key(create=False)}


@router.post("/enroll-key/rotate")
def rotate_key(user=Depends(require_install("act.connect")), svc=Depends(services)):
    return {"key": svc.agents.rotate_enroll_key()}


@router.delete("/enroll-key")
def disable_key(user=Depends(require_install("act.connect")), svc=Depends(services)):
    svc.agents.disable_enroll_key()
    return {"ok": True}


@router.get("/install-info")
def install_info(user=Depends(current_user), svc=Depends(services)):
    """The current one-line install commands: {BASE} is the receiver address the panel uses, {AUTH} the --enroll-key / --token
    part. Fingerprints only (release key, boot.sh, edge CA): nothing secret."""
    sc = getattr(svc, "spool_consumer", None)
    return svc.agent_updates.install_info(str(sc.dir) if sc is not None else None)


@router.get("/updates")
def update_status(svc=Depends(services), _u=Depends(perm_scope("page.conn"))):
    """Self-update: the signed release, the global switch "Ajanlar kendini güncellesin", the rollout share, keyd's releases."""
    return {**svc.agent_updates.status(), "versions": svc.agent_updates.versions()}


@router.put("/updates")
def update_settings(body: UpdateSettingsIn, user=Depends(require_install("act.connect")), svc=Depends(services)):
    svc.agent_updates.set_settings(body.enabled, body.pct)
    return svc.agent_updates.status()


@router.post("/{aid}/update/pause")
def update_pause(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _agent(svc, acc, aid, "act.connect")
    svc.agent_updates.set_paused(aid, True)
    return _with_team(svc, [svc.agents.get(aid)])[0]


@router.post("/{aid}/update/resume")
def update_resume(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _agent(svc, acc, aid, "act.connect")
    svc.agent_updates.set_paused(aid, False)
    return _with_team(svc, [svc.agents.get(aid)])[0]


@router.post("/{aid}/update/pin")
def update_pin(aid: int, body: PinIn, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    """"Bu sürüme döndür": keyd signs a pin bound to this host for a version it signed before (empty version: clear the pin).
    The agent switches to it from the binary it kept, older or not; without a pin it never accepts an older build."""
    _agent(svc, acc, aid, "act.connect")
    svc.agent_updates.set_pin(aid, body.version)
    return _with_team(svc, [svc.agents.get(aid)])[0]


@router.get("/{aid}")
def get(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("page.conn"))):
    return _with_team(svc, [_agent(svc, acc, aid, "page.conn")])[0]


@router.post("/{aid}/revoke")
def revoke(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _agent(svc, acc, aid, "act.connect")
    svc.agents.revoke(aid); return svc.agents.get(aid)


@router.post("/{aid}/reactivate")
def reactivate(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _agent(svc, acc, aid, "act.connect")
    svc.agents.reactivate(aid); return svc.agents.get(aid)


def _take_over(svc, acc, a: dict) -> None:
    """Rotating hands out a token. A team-scoped account rotates only its own team's agents; an installation-wide agent (fleet
    key, no team) on one of its hosts first becomes its team's agent, so the new token posts only for the team's hosts
    (ingest_guard). Another team's agent (a host both teams see) answers 404."""
    if acc.wide:
        return
    mine = acc.team_ids("act.connect")
    if a.get("team_id") in mine:
        return
    tid = next((m.team_id for m in acc.memberships if m.team_id in mine and a["name"] in m.hosts), None) if a.get("team_id") is None else None
    if tid is None:
        raise KeyError(a["id"])
    svc.agents.set_team(a["id"], tid)


@router.post("/{aid}/rotate")
def rotate(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _take_over(svc, acc, _agent(svc, acc, aid, "act.connect"))
    return {"token": svc.agents.rotate(aid)}


@router.delete("/{aid}")
def delete(aid: int, acc=Depends(access), svc=Depends(services), _u=Depends(perm_scope("act.connect"))):
    _agent(svc, acc, aid, "act.connect")
    svc.agents.delete(aid); return {"ok": True}


@router.post("/prune")
def prune(days: int = 7, user=Depends(require_install("act.connect")), svc=Depends(services)):
    """Agents not heard from for `days` days go (their tokens with them); Watchover's own built-in agent stays. Audited."""
    from ... import selfmon
    keep = tuple(a["id"] for a in svc.agents.list() if selfmon.is_self(a))
    gone = svc.agents.prune(days, keep)
    try:
        svc.users._event(user.get("email", ""), "agents-prune", True, f"{len(gone)} agents not seen for {days} days removed: "
                                                                     + ", ".join(a["name"] for a in gone)[:400])
    except Exception:  # noqa: BLE001
        pass
    return {"removed": gone}
