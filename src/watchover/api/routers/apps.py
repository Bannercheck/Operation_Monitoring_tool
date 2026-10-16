"""Business applications ("Uygulamalar", watchover.applications): the list with health, one application in detail, create / edit /
delete, membership, discovery proposals (accept, dismiss), and per application its time series, service map, traces, events
(anomalies) and threshold view; "claim hosts" runs the application's hosts through the team gate (Teams.claim) for its owning team.

Permissions: page.apps to read, act.apps to write. An installation-wide application (team_id NULL) needs act.apps installation-wide,
a team's application act.apps in that team. Another team's application does not exist for a reader (404 everywhere). Traces and the
service map also need page.apm (their spans are filtered to the reader's own hosts / service rules as on the APM pages)."""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...applications import CRITICALITY, KINDS, NameTaken
from ..security import access, require, services

router = APIRouter(prefix="/apps", tags=["applications"])


class AppIn(BaseModel):
    name: str
    description: str = ""
    team_id: Optional[int] = None     # Optional[]: pydantic evaluates these at runtime (Python 3.9)
    criticality: str = "normal"
    tags: List[str] = []
    slo_availability: Optional[float] = None
    slo_p95_ms: Optional[float] = None
    members: Dict[str, list] = {}


class MembersIn(BaseModel):
    hosts: Optional[List[str]] = None
    inv_apps: Optional[List[str]] = None
    pools: Optional[List[str]] = None
    services: Optional[List[str]] = None
    namespaces: Optional[List[str]] = None
    rum_apps: Optional[List[str]] = None
    flows: Optional[List[int]] = None


class AssignIn(BaseModel):
    hosts: List[str] = []
    services: List[str] = []


class AcceptIn(BaseModel):
    key: str
    name: Optional[str] = None
    team_id: Optional[int] = None
    into: Optional[int] = None        # merge into this existing application instead (an explicit choice, never automatic)


class DismissIn(BaseModel):
    key: str


# ---------------------------------------------------------------- helpers
def _sc(acc):
    return acc.scope("page.apps")


def _app(svc, acc, aid: int) -> dict:
    a = svc.apps.get(aid)
    if not svc.apps.visible(a, _sc(acc)):
        raise KeyError(f"application {aid}")
    return a


def _may_write(acc, team_id: Optional[int]) -> bool:
    return acc.install("act.apps") if team_id is None else acc.can_in(team_id, "act.apps")


def _writable(svc, acc, aid: int) -> dict:
    a = _app(svc, acc, aid)
    if not _may_write(acc, a.get("team_id")):
        raise HTTPException(403, "permission required to change this application: act.apps")
    return a


def _team_for(svc, acc, team_id: Optional[int]) -> Optional[int]:
    """The owning team of a new (or re-owned) application: installation-wide needs act.apps installation-wide; a team needs act.apps
    there (an installation-wide manager may name any team). A team-scoped caller who leaves it out gets their only such team."""
    if team_id is None:
        if acc.install("act.apps"):
            return None
        ids = acc.team_ids("act.apps")
        if len(ids) == 1:
            return ids[0]
        if not ids:
            raise HTTPException(403, "permission required: act.apps")
        raise HTTPException(400, "team_id required: you hold act.apps in several teams")
    if acc.install("act.apps"):
        svc.teams.get(int(team_id))                          # unknown team -> 404
        return int(team_id)
    if acc.can_in(int(team_id), "act.apps"):
        return int(team_id)
    raise HTTPException(403, "permission required in that team: act.apps")


def _check_hosts(acc, hosts) -> None:
    """A team-scoped editor names only hosts their scope holds (membership never opens a host; this keeps names they cannot see
    out of their records)."""
    sc = acc.scope("act.apps")
    if sc is None:
        return
    bad = [h for h in hosts or [] if not sc.allows(h)]
    if bad:
        raise HTTPException(400, f"hosts outside your team's scope: {', '.join(bad[:5])}")


def _save(svc, data: dict, aid: Optional[int] = None, by: str = "") -> dict:
    try:
        return svc.apps.save(data, aid, by=by)
    except NameTaken as e:
        raise HTTPException(409, str(e))


def _envs(svc, hosts) -> list:
    uni = svc.apps._hosts_universe()
    return sorted({(uni.get(h) or {}).get("env") or "" for h in hosts} - {""})


def _row(svc, acc, a: dict, health: Optional[dict]) -> dict:
    sc = _sc(acc)
    hosts, _s, _h = svc.apps.scoped(a, sc)
    return {**svc.apps.view(a, sc, _may_write(acc, a.get("team_id"))), "envs": _envs(svc, hosts), "health": health}


# ---------------------------------------------------------------- list and catalogue
@router.get("")
def list_apps(minutes: int = Query(60, ge=5, le=60), team_id: Optional[int] = None, env: str = "", criticality: str = "", q: str = "",
              acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Every application the reader may see, with its health over the last `minutes` (filters: team, environment, criticality, text)."""
    sc = _sc(acc)
    apps = svc.apps.visible_list(sc)
    if team_id is not None:
        apps = [a for a in apps if a.get("team_id") == team_id]
    if criticality:
        apps = [a for a in apps if a["criticality"] == criticality]
    if q:
        ql = q.strip().lower()
        apps = [a for a in apps if ql in a["name"].lower() or ql in (a.get("description") or "").lower() or any(ql in t.lower() for t in a["tags"])]
    health = svc.apps.health(apps, sc, minutes) if apps else {}
    rows = [_row(svc, acc, a, health.get(a["id"])) for a in apps]
    if env:
        rows = [r for r in rows if env in r["envs"]]
    order = {"crit": 0, "warn": 1, "ok": 2, "nodata": 3}
    rows.sort(key=lambda r: (order.get((r["health"] or {}).get("status"), 4), CRITICALITY.index(r["criticality"]), r["name"].lower()))
    return {"apps": rows, "window_min": minutes, "can_create": acc.install("act.apps") or bool(acc.team_ids("act.apps")),
            "envs": sorted({e for r in rows for e in r["envs"]}),
            "teams": [{"id": i, "name": n} for i, n in sorted({(r["team_id"], r["team"]) for r in rows if r["team_id"] is not None}, key=lambda x: x[1])]}


@router.get("/catalog")
def catalog(acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """What the editor offers: the hosts, inventory application values, pools, services, namespaces, RUM apps and business flows the
    reader may see, the teams an application may be given to, and the criticality levels."""
    sc = _sc(acc)
    out = svc.apps.catalog(sc)
    try:
        out["rum_apps"] = sorted(a["name"] for a in svc.rum.apps() if sc is None or sc.allows_service(a["name"]))
    except Exception:  # noqa: BLE001
        out["rum_apps"] = []
    try:
        teams = None if acc.wide else set(acc.team_ids("page.apps"))
        out["flows"] = [{"id": f["id"], "name": f["name"]} for f in svc.bizflows.list() if teams is None or f.get("team_id") is None or f["team_id"] in teams]
    except Exception:  # noqa: BLE001
        out["flows"] = []
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    ids = list(names) if acc.install("act.apps") else acc.team_ids("act.apps")
    out["teams"] = [{"id": i, "name": names[i]} for i in ids if i in names]
    out["install"] = acc.install("act.apps")
    out["criticality"] = list(CRITICALITY)
    return out


# ---------------------------------------------------------------- discovery
def _owner(acc, user: dict) -> str:
    return "*" if acc.wide else "u:" + (user.get("email") or "").lower()


@router.get("/discovery")
def discovery(acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Proposals from inventory application values, OTel service.namespace and service-name groups, and the hosts / services no
    application covers (all within the reader's scope). Nothing is created or merged until accepted."""
    return svc.apps.discover(_sc(acc), _owner(acc, user))


@router.post("/discovery/accept", status_code=201)
def accept(body: AcceptIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    sc = _sc(acc)
    prop = next((p for p in svc.apps.discover(sc, _owner(acc, user))["proposals"] if p["key"] == body.key), None)
    if prop is None:
        raise KeyError(f"proposal {body.key}")
    if body.into is not None:                                  # explicit merge into an existing application
        _writable(svc, acc, body.into)
        return svc.apps.add_members(body.into, prop["members"])
    team = _team_for(svc, acc, body.team_id)
    return _save(svc, {"name": (body.name or prop["name"]).strip(), "team_id": team, "members": prop["members"],
                       "description": {"inventory": "Envanterden önerildi", "otel": "OpenTelemetry service.namespace'ten önerildi",
                                       "services": "Servis adlarından önerildi"}.get(prop["source"], "")}, by=user.get("email", ""))


@router.post("/discovery/dismiss")
def dismiss(body: DismissIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    svc.apps.dismiss(body.key, _owner(acc, user))
    return {"ok": True}


# ---------------------------------------------------------------- CRUD
@router.post("", status_code=201)
def create(body: AppIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    d = body.model_dump()
    d["team_id"] = _team_for(svc, acc, body.team_id)
    _check_hosts(acc, (d.get("members") or {}).get("hosts"))
    return _save(svc, d, by=user.get("email", ""))


@router.get("/{aid}")
def app_detail(aid: int, minutes: int = Query(60, ge=5, le=60), acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """One application: the record, its health, member hosts (with how they joined, state, threshold level), hosts reached through
    its services, services (log and trace numbers), namespaces, RUM apps, business flows and the log query that selects it."""
    a = _app(svc, acc, aid)
    sc = _sc(acc)
    det = svc.apps.detail(a, sc, minutes)
    return {**svc.apps.view(a, sc, _may_write(acc, a.get("team_id"))), **det, "envs": _envs(svc, [h["host"] for h in det["hosts"]]),
            "can_claim": a.get("team_id") is not None and acc.manages(a["team_id"])}


@router.put("/{aid}")
def update(aid: int, body: AppIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    cur = _writable(svc, acc, aid)
    d = body.model_dump()
    d["team_id"] = cur.get("team_id") if "team_id" not in body.model_fields_set or body.team_id == cur.get("team_id") else _team_for(svc, acc, body.team_id)
    if "members" not in body.model_fields_set:
        d["members"] = cur["members"]
    else:
        _check_hosts(acc, [h for h in (d["members"] or {}).get("hosts") or [] if h not in cur["members"]["hosts"]])
        if _sc(acc) is not None:                              # explicit hosts the editor cannot see stay as they were
            hidden = [h for h in cur["members"]["hosts"] if not _sc(acc).allows(h)]
            d["members"] = {**d["members"], "hosts": list(dict.fromkeys(list(d["members"].get("hosts") or []) + hidden))}
    return _save(svc, d, aid)


@router.delete("/{aid}")
def delete(aid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    _writable(svc, acc, aid)
    return {"ok": True, "policies_disabled": svc.apps.delete(aid)}


@router.put("/{aid}/members")
def set_members(aid: int, body: MembersIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    """Replace the membership lists given (the others stay)."""
    cur = _writable(svc, acc, aid)
    m = dict(cur["members"])
    for k in KINDS:
        v = getattr(body, k)
        if v is not None:
            m[k] = v
    if body.hosts is not None:
        _check_hosts(acc, [h for h in body.hosts if h not in cur["members"]["hosts"]])
        sc = _sc(acc)
        if sc is not None:
            m["hosts"] = list(dict.fromkeys(list(body.hosts) + [h for h in cur["members"]["hosts"] if not sc.allows(h)]))
    return _save(svc, {**cur, "members": m}, aid)


@router.post("/{aid}/assign")
def assign(aid: int, body: AssignIn, acc=Depends(access), svc=Depends(services), user=Depends(require("act.apps"))):
    """Add unmatched hosts / services (discovery) to an application."""
    _writable(svc, acc, aid)
    _check_hosts(acc, body.hosts)
    sc = _sc(acc)
    if sc is not None:
        su = svc.apps._service_universe()
        bad = [s for s in body.services if not sc.allows_service(s, su.get(s))]
        if bad:
            raise HTTPException(400, f"services outside your team's scope: {', '.join(bad[:5])}")
    try:
        return svc.apps.add_members(aid, {"hosts": body.hosts, "services": body.services})
    except NameTaken as e:
        raise HTTPException(409, str(e))


@router.post("/{aid}/claim-hosts")
def claim_hosts(aid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Owning an application does not open its hosts to the team; this runs its member hosts through the team gate (Teams.claim):
    the team's own and brand-new hosts join, another team's host never does, a known unowned host only for an installation admin
    (or with teams_self_claim). Needs team.manage in the owning team (or page.teams installation-wide)."""
    from ... import settings as wo_settings
    a = _app(svc, acc, aid)
    tid = a.get("team_id")
    if tid is None:
        raise HTTPException(400, "an installation-wide application has no owning team")
    if not acc.manages(tid):
        raise HTTPException(403, "permission required: team.manage in the owning team")
    hosts = sorted(svc.apps.members(a).hosts)
    admin = "page.teams" in acc.global_perms
    added, rejected = svc.teams.claim(tid, hosts, "application", by=user.get("id"), admin=admin,
                                      self_claim=bool(wo_settings.load().get("teams_self_claim")))
    sc = acc.scope(None)
    shown = [r for r in rejected if sc is None or sc.allows(r["host"])]
    if added:
        try:
            svc.users._event(user.get("email", ""), "team", True, f"{svc.teams.get(tid)['name']}: {len(added)} host(s) claimed via application {a['name']}"[:200])
        except Exception:  # noqa: BLE001
            pass
    return {"added": added, "rejected": shown, "rejected_hidden": len(rejected) - len(shown)}


# ---------------------------------------------------------------- per application views
@router.get("/{aid}/series")
def series(aid: int, minutes: int = Query(60, ge=5, le=10080), acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Per bucket: log lines and ERROR+ (live counters up to 60 min, the history beyond), trace calls, errors and p95."""
    a = _app(svc, acc, aid)
    return svc.apps.series(a, _sc(acc), minutes)


def _need_apm(acc) -> None:
    if not acc.can("page.apm"):
        raise HTTPException(403, "permission required: page.apm")


@router.get("/{aid}/topology")
def topology(aid: int, minutes: int = Query(60, ge=5, le=10080), acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """The service map cut to the application's services and their direct neighbours, and the agents' network flows of its hosts."""
    from .apm import _cut, _svc_ok
    _need_apm(acc)
    a = _app(svc, acc, aid)
    hosts, svc_ok, _h = svc.apps.scoped(a, _sc(acc))
    top = _cut(svc.traces.topology(minutes), svc_ok)
    asc = acc.scope("page.apm")
    ok = _svc_ok(svc, asc, None)
    if ok is not None:
        top = _cut(top, ok)
    try:
        net = _cut(svc.flows.graph(minutes, None), lambda h: h in hosts)
        if asc is not None:
            net = _cut(net, asc.allows)
    except Exception:  # noqa: BLE001
        net = {"nodes": [], "edges": []}
    for n in top["nodes"]:
        n["member"] = bool(svc_ok(n["id"]))
    return {"services": top, "network": net}


@router.get("/{aid}/traces")
def traces(aid: int, minutes: int = Query(60, ge=5, le=10080), errors: bool = False, order: str = Query("recent", pattern="^(recent|duration)$"),
           limit: int = Query(50, ge=1, le=200), acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Recent (or slowest) traces through the application's services; a team sees only traces with spans of its own."""
    from .apm import _traces_with_own_spans
    _need_apm(acc)
    a = _app(svc, acc, aid)
    det = svc.apps.detail(a, _sc(acc), min(minutes, 60))
    names = [s["service"] for s in det["services"] if s["member"] and (s["calls"] or s["lines"])][:15]
    seen, rows = set(), []
    for s in names:
        for h in svc.traces.search(service=s, minutes=minutes, errors=errors, order=order, limit=limit):
            if h["trace_id"] not in seen:
                seen.add(h["trace_id"]); rows.append(h)
    asc = acc.scope("page.apm")
    if asc is not None and rows:
        mine = _traces_with_own_spans(svc.traces, asc, [r["trace_id"] for r in rows])
        rows = [r for r in rows if r["trace_id"] in mine]
    rows.sort(key=(lambda r: -r["duration_ms"]) if order == "duration" else (lambda r: r["start"]), reverse=order != "duration")
    return {"services": names, "traces": rows[:limit]}


@router.get("/{aid}/events")
def events(aid: int, minutes: int = Query(1440, ge=5, le=43200), limit: int = Query(200, ge=1, le=500), acc=Depends(access), svc=Depends(services),
           user=Depends(require("page.apps"))):
    """Anomalies (threshold breaches included) on the application's hosts and services: open ones and those seen in the range."""
    a = _app(svc, acc, aid)
    return {"events": svc.apps.events(a, _sc(acc), minutes, limit)}


@router.get("/{aid}/thresholds")
def thresholds(aid: int, acc=Depends(access), svc=Depends(services), user=Depends(require("page.apps"))):
    """Open threshold states on the member hosts, the policies that name this application (with whether the reader may edit them),
    and the effective levels per member host (thresholds overview)."""
    a = _app(svc, acc, aid)
    sc = _sc(acc)
    out = svc.apps.thresholds_view(a, sc)
    hosts = set(out["hosts"])
    out["overview"] = svc.thresholds.overview(lambda h: h in hosts) if hosts else []
    mine = None if acc.wide else set(acc.team_ids("page.thr"))
    for p in out["policies"]:
        tid = p.get("team_id")
        p["editable"] = acc.install("sys.notify") or (tid is not None and acc.can_in(tid, "act.thresholds"))
        p["visible"] = mine is None or tid is None or tid in mine
    out["can_add"] = acc.install("sys.notify") or bool(acc.team_ids("act.thresholds"))
    return out
