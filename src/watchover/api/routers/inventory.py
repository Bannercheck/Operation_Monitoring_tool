"""Inventory: the hosts Watchover knows by name. A team-scoped account reads only its teams' hosts (list, stats, export, one host)
and writes only where its team role (or installation role) grants act.inventory; a host it creates or promotes becomes the
team's own (teams.claim: never another team's host)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from ..security import access, claim_host, current_user, host_guard, perm_scope, pick_team, require, services

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _annotate(svc, acc, rows: list, key: str = "hostname") -> list:
    """team_ids: the caller's teams that see the host (edit buttons follow the team role); installation-wide callers also get the
    team names (and unowned hosts are marked when teams exist)."""
    if acc.wide:
        teams = svc.teams.list()
        if not teams:
            return rows
        by: dict = {}
        for t in teams:
            for h in t["matched"]:
                by.setdefault(h, []).append(t["name"])
        return [{**r, "teams": by.get(r[key], []), "unowned": r[key] not in by} for r in rows]
    return [{**r, "team_ids": [m.team_id for m in acc.memberships if r[key] in m.hosts]} for r in rows]


@router.get("")
def list_hosts(q: str = "", acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.inv"))):
    rows = svc.inventory.list(q)
    rows = rows if sc is None else [r for r in rows if sc.allows(r["hostname"])]
    return _annotate(svc, acc, rows)


@router.get("/stats")
def stats(svc=Depends(services), sc=Depends(perm_scope("page.inv"))):
    return svc.inventory.stats(svc.live.hosts(), None if sc is None else sc.allows)


@router.get("/discovered")
def discovered(acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.inv"))):
    rows = svc.inventory.discovered(svc.live.hosts(), svc.agents.list())
    rows = rows if sc is None else [r for r in rows if sc.allows(r["hostname"])]     # a team sees only its own servers, as on the list
    return _annotate(svc, acc, rows)


@router.get("/export", response_class=PlainTextResponse)
def export(svc=Depends(services), sc=Depends(perm_scope("page.inv"))):
    return PlainTextResponse(svc.inventory.export_csv(None if sc is None else sc.allows).decode("utf-8"), media_type="text/csv")


@router.get("/template", response_class=PlainTextResponse)
def template(user=Depends(require("page.inv"))):
    from ...inventory import Inventory
    return PlainTextResponse(Inventory.csv_template().decode("utf-8"), media_type="text/csv")


@router.post("/import")
async def import_csv(file: UploadFile = File(...), team_id: Optional[int] = None, user=Depends(current_user), acc=Depends(access),
                     svc=Depends(services), _u=Depends(require("act.inventory"))):
    """CSV rows: an existing host must be in the caller's act.inventory scope, a new one is claimed for the team (`team_id` when the
    caller holds act.inventory in several teams). Refused rows land in `errors`."""
    sc = acc.scope("act.inventory")
    if not acc.wide:
        pick_team(acc, "act.inventory", team_id)                     # 400 / 403 before anything is read

    def check(rec: dict) -> None:
        h = str(rec.get("hostname", "")).strip().lower()
        if not h:
            raise ValueError("hostname is required")
        if svc.inventory.get(h):
            if sc is not None and not sc.allows(h):
                raise ValueError(f"{h}: outside your team scope")
            return
        try:
            claim_host(svc, acc, user, "act.inventory", h, "import", team_id)
        except HTTPException as e:
            raise ValueError(f"{h}: {e.detail}")
    n, errors = svc.inventory.import_csv(await file.read(), check if (sc is not None or team_id is not None) else None)
    return {"imported": n, "errors": errors}


@router.post("/promote")
def promote(body: dict, user=Depends(current_user), acc=Depends(access), svc=Depends(services), _u=Depends(require("act.inventory"))):
    """Bulk promote of discovered hosts ({"hosts": [{hostname, env, ip, dc}], "criticality", "team_id"?}): one call per pool or
    selection. A team-scoped account promotes only hosts of its own scope (the list it was shown); they become the team's own."""
    hosts = body.get("hosts") or []
    sc = acc.scope("act.inventory")
    names = [str((x or {}).get("hostname") or "").strip().lower() for x in hosts if isinstance(x, dict)] if isinstance(hosts, list) else []
    if sc is not None and any(not sc.allows(h) for h in names):
        raise HTTPException(403, "host outside your team scope")
    tid = body.get("team_id")
    if not acc.wide:
        pick_team(acc, "act.inventory", tid)
    n = svc.inventory.promote(hosts, str(body.get("criticality") or "normal"))
    if not acc.wide or tid is not None:
        for h in names:
            if h:
                claim_host(svc, acc, user, "act.inventory", h, "promote", tid)
    return {"added": n}


@router.put("", status_code=201)
def upsert(rec: dict, user=Depends(current_user), acc=Depends(access), svc=Depends(services), _u=Depends(require("act.inventory"))):
    """Create or update by hostname. Updating: the host must be in the caller's act.inventory scope. Creating: the host is claimed
    for the caller's team (409 when another team sees it, 403 when it is known but unowned)."""
    rec = dict(rec)
    team_id = rec.pop("team_id", None)
    rec.pop("team_ids", None); rec.pop("teams", None); rec.pop("unowned", None)
    h = str(rec.get("hostname", "")).strip().lower()
    if not h:
        raise ValueError("hostname is required")
    if svc.inventory.get(h):
        host_guard(acc, h, "act.inventory", "page.inv")
    else:
        claim_host(svc, acc, user, "act.inventory", h, "inventory", int(team_id) if team_id not in (None, "") else None)
    return _annotate(svc, svc.access_of(user), [svc.inventory.upsert(rec)])[0]


@router.get("/{hostname}")
def get(hostname: str, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.inv"))):
    r = svc.inventory.get(hostname)
    if not r or (sc is not None and not sc.allows(r["hostname"])):
        raise KeyError(hostname)
    return _annotate(svc, acc, [r])[0]


@router.delete("/{hostname}")
def delete(hostname: str, acc=Depends(access), svc=Depends(services), _u=Depends(require("act.inventory"))):
    """Removes the record (not the team's ownership of the host)."""
    r = svc.inventory.get(hostname)
    if not r:
        raise KeyError(hostname)
    host_guard(acc, r["hostname"], "act.inventory", "page.inv")
    svc.inventory.delete(hostname); return {"ok": True}
