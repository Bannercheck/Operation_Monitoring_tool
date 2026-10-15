"""APM: services (RED, percentiles, Apdex), one service in detail (operations, database and external calls, slow traces),
trace search and the waterfall of one trace. `dataset=<id>` runs the same views over an uploaded dataset instead of the live feed."""
from __future__ import annotations

import re
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...traces import store_for_observations
from ..security import access_of, require, services, user_scope

from .datasets import dataset_query_guard

router = APIRouter(tags=["apm"], dependencies=[Depends(dataset_query_guard)])   # ?dataset=<another team's id> -> 404
_cache: dict[str, tuple[int, object]] = {}
_lock = threading.Lock()


def _store(svc, dataset: str | None):
    if not dataset or dataset == "live":
        return svc.traces, 60
    d = svc.datasets.get(dataset)
    a = d["analysis"]
    with _lock:
        hit = _cache.get(dataset)
        if hit and hit[0] == id(a):
            return hit[1], None
        st = store_for_observations(a.observations, settings=None)
        _cache[dataset] = (id(a), st)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
        return st, None


def _svc_ok(svc, sc, dataset):
    """None when everything is visible, else a predicate service-name -> visible for this user's teams (live feed only)."""
    if sc is None or (dataset and dataset != "live"):
        return None
    sh = svc.traces.service_hosts(10080)
    try:
        for o in svc.live.snapshot():
            if o.service and o.host:
                sh.setdefault(o.service, set()).add(o.host)
    except Exception:  # noqa: BLE001
        pass
    return lambda name: sc.allows_service(name, sh.get(name))


def _own_span(sc, r: dict) -> bool:
    """A span is the team's when it ran on one of the team's hosts or its service matches one of the team's service rules. A
    service name shared with another team (any agent may emit lines under any service name) does not open that team's spans."""
    return sc.allows(r.get("host")) or sc.allows_service(r.get("service") or "")


def _traces_with_own_spans(st, sc, ids: list) -> set:
    out: set = set()
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in st.db._exec(f"SELECT DISTINCT trace_id, service, host FROM spans WHERE trace_id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
            if r["trace_id"] not in out and _own_span(sc, r):
                out.add(r["trace_id"])
    return out


def _minutes(minutes: int | None, default: int | None) -> int | None:
    return default if minutes is None else (minutes or None)


@router.get("/apm/services")
def services_list(minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    rows = st.services(_minutes(minutes, dflt), env or None)
    return {"services": [r for r in rows if ok is None or ok(r["service"])], "received": getattr(st, "received", 0), "kept": getattr(st, "kept", 0)}


@router.get("/apm/topology")
def topology(minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """Service map: trace edges (calls, errors, latency, explicit / inferred) and, for the live feed, the agents' network flows.
    For a team: its services and hosts plus the direct neighbours they talk to (shown, so the boundary is visible)."""
    st, dflt = _store(svc, dataset)
    m = _minutes(minutes, dflt)
    net = svc.flows.graph(m, env or None) if (not dataset or dataset == "live") and getattr(svc, "flows", None) is not None else {"nodes": [], "edges": []}
    top = st.topology(m, env or None)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None:
        top = _cut(top, ok)
        net = _cut(net, sc.allows)
    return {"services": top, "network": net}


def _cut(g: dict, ok) -> dict:
    edges = [e for e in g["edges"] if ok(e["source"]) or ok(e["target"])]
    keep = {x for e in edges for x in (e["source"], e["target"])} | {n["id"] for n in g["nodes"] if ok(n["id"])}
    return {**g, "edges": edges, "nodes": [n for n in g["nodes"] if n["id"] in keep]}


@router.get("/apm/hosts")
def integration_hosts(minutes: int = 1440, env: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """Hosts with integrations (from agent network flows): how many inbound and outbound, which protocols."""
    rows = svc.flows.hosts(minutes or None, env or None)
    return rows if sc is None else [h for h in rows if sc.allows(h["host"])]


@router.get("/apm/hosts/{host}/integrations")
def host_integrations(host: str, request: Request, minutes: int = 1440, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    if sc is not None and not sc.allows(host):
        raise HTTPException(404, "host not found")
    """One host's integration map: callers, listening ports with processes, outbound integrations by category, and the
    services seen on that host in the traces."""
    acc = access_of(request, user)
    teams = None if acc.wide else set(acc.team_ids("page.apm"))
    m = svc.flows.host_map(host, minutes or None, visible=None if sc is None else sc.allows,    # technology: evidence only from hosts the reader sees
                           manual_ok=None if teams is None else (lambda t: t is None or t in teams))
    ts = getattr(svc, "techstack", None)
    if ts is not None:
        m["services"] = ts.services(host, minutes or None)
    else:
        rows = svc.kb._exec("SELECT DISTINCT service FROM spans WHERE host = ? AND kind NOT IN ('user','mobile','log') LIMIT 50", (host,))
        m["services"] = sorted(r["service"] for r in rows)
    return m


class TechOverride(BaseModel):
    host: str
    target: str
    port: int
    protocol: Optional[str] = None
    client: Optional[str] = None
    server: Optional[str] = None
    note: Optional[str] = None
    team_id: Optional[int] = None


def _integ_team(svc, acc, host: str, target: str, team_id: Optional[int]) -> Optional[int]:
    """Who may set an integration's technology by hand, and for which team: act.inventory on either end of the integration
    (404 when the reader cannot even see it). The row belongs to the caller's team (installation-wide for installation-wide users,
    unless they name a team)."""
    sc = acc.scope("act.inventory")
    if sc is not None and not (sc.allows(host) or sc.allows(target)):
        rs = acc.scope("page.apm")
        if rs is None or rs.allows(host) or rs.allows(target):
            raise HTTPException(403, "permission required on that integration: act.inventory")
        raise HTTPException(404, "integration not found")
    if acc.wide:
        if team_id is not None:
            svc.teams.get(int(team_id))                      # unknown team -> 404
        return team_id
    ids = [m.team_id for m in acc.memberships if acc.can_in(m.team_id, "act.inventory") and (host in m.hosts or target in m.hosts)]
    if team_id is not None:
        if team_id not in ids:
            raise HTTPException(403, "permission required in that team: act.inventory")
        return team_id
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise HTTPException(403, "permission required: act.inventory")
    raise HTTPException(400, "team_id required: the integration is in several of your teams")


def _clip(v: Optional[str], n: int = 80) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", (v or "").strip())[:n]


@router.put("/apm/integrations/tech")
def set_integration_tech(body: TechOverride, request: Request, user=Depends(require("act.inventory")), svc=Depends(services)):
    """Set by hand which technology one integration runs on (protocol / client / server / note): wins over every guess, shown as
    "manual" with who and when. One row per team; the reader sees their own team's row, else the installation-wide one."""
    from ...privacy import pseudonym, scrub
    host, target = _clip(body.host, 120), _clip(body.target, 120)
    if not host or not target or not 0 < int(body.port) < 65536:
        raise HTTPException(400, "host, target and port are required")
    vals = {"protocol": _clip(body.protocol), "client": _clip(body.client), "server": _clip(body.server), "note": scrub(_clip(body.note, 300))}
    if not any(vals.values()):
        raise HTTPException(400, "nothing to set: protocol, client, server or note")
    tid = _integ_team(svc, access_of(request, user), host, target, body.team_id)
    return svc.techstack.set_override(host, target, int(body.port), tid, pseudonym(user.get("email") or ""), **vals)


@router.delete("/apm/integrations/tech")
def delete_integration_tech(host: str, target: str, port: int, request: Request, team_id: Optional[int] = None, user=Depends(require("act.inventory")),
                            svc=Depends(services)):
    """Back to the evidence: removes the caller's team's (or, installation-wide, the installation's) override of one integration."""
    tid = _integ_team(svc, access_of(request, user), host, target, team_id)
    n = svc.techstack.delete_override(host, target, int(port), tid)
    if not n:
        raise HTTPException(404, "no manual technology for that integration")
    return {"ok": True, "deleted": n}


@router.get("/apm/services/{name:path}")
def service_detail(name: str, minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None and not ok(name):
        raise HTTPException(404, "service not found")
    return st.service(name, _minutes(minutes, dflt), env or None)


@router.get("/traces")
def trace_search(service: str | None = None, operation: str | None = None, minutes: int | None = None, env: str | None = None, min_ms: float | None = None,
                 errors: bool = False, q: str | None = None, session: str | None = None, user_ref: str | None = None, order: str = "recent", limit: int = 50,
                 dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None:
        hits = st.search(service or None, operation or None, _minutes(minutes, dflt), env or None, min_ms, errors, q or None, session or None, user_ref or None, order, 500)
        mine = _traces_with_own_spans(st, sc, [h["trace_id"] for h in hits])
        return [h for h in hits if h["trace_id"] in mine][:limit]
    return st.search(service or None, operation or None, _minutes(minutes, dflt), env or None, min_ms, errors, q or None, session or None, user_ref or None, order, limit)


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, _ = _store(svc, dataset)
    if sc is not None and (not dataset or dataset == "live"):                 # a team sees the spans of its own hosts / service rules only
        from ...traces import waterfall
        rows = [r for r in st.db._exec("SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ms, duration_ms DESC", (trace_id,)) if _own_span(sc, r)]
        t = waterfall(rows) if rows else None
    else:
        t = st.trace(trace_id)
    if t is None:
        raise HTTPException(404, "trace not found (outside the retention window or not sampled)")
    return t


_STEP_KINDS = ("user", "mobile")


@router.get("/traces/{trace_id}/session")
def trace_session(trace_id: str, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """The client session behind one trace: the browser / app session (device, the user step that started this request and the
    steps around it) when the sensors saw it, otherwise the server-side session from the logs (its requests, hosts, services).
    Ids stay pseudonyms; on the live store a team sees only its own spans of the session (as in trace_detail)."""
    import json
    from datetime import datetime, timezone
    st, _ = _store(svc, dataset)
    live = not dataset or dataset == "live"
    own = (lambda r: True) if sc is None or not live else (lambda r: _own_span(sc, r))
    mine = [r for r in st.db._exec("SELECT session_id, kind, host, service FROM spans WHERE trace_id = ?", (trace_id,)) if own(r)]
    if not mine:
        raise HTTPException(404, "trace not found")
    sid = next((r["session_id"] for r in sorted(mine, key=lambda r: r["kind"] not in _STEP_KINDS) if r["session_id"]), "")
    if not sid:
        return {"session": None}
    rows = [r for r in st.db._exec("SELECT trace_id, start_ms, duration_ms, service, operation, kind, host, error, user_ref, attrs FROM spans "
                                   "WHERE session_id = ? ORDER BY start_ms LIMIT 3000", (sid,)) if own(r)]
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None   # noqa: E731
    out: dict = {"session": sid, "user_ref": next((r["user_ref"] for r in rows if r["user_ref"]), ""), "kind": "server", "capped": len(rows) >= 3000}
    rum = svc.rum.kb._exec("SELECT * FROM rum_sessions WHERE session_id = ?", (sid,)) if live else []
    if rum:
        steps = []
        for r in rows:
            if r["kind"] not in _STEP_KINDS:
                continue
            a = json.loads(r["attrs"]) if r.get("attrs") else {}
            steps.append({"step": a.get("step") or r["kind"], "name": r["operation"], "ts": iso(r["start_ms"]), "error": int(r["error"] or 0),
                          "trace_id": r["trace_id"], "this": r["trace_id"] == trace_id})
        at = next((i for i, s in enumerate(steps) if s["this"]), None)
        r0 = rum[0]
        out.update({k: r0[k] for k in ("app", "platform", "version", "os", "browser", "model", "net", "carrier", "started", "last", "duration_ms",
                                       "views", "actions", "requests", "errors", "crashes", "entry", "exit")})
        out.update({"kind": "rum", "user_ref": r0["user_ref"] or out["user_ref"], "trigger": steps[at] if at is not None else None,
                    "position": at + 1 if at is not None else None, "steps_total": len(steps),
                    "around": steps[max(0, at - 3):at + 4] if at is not None else steps[-5:]})
        return out
    order = list(dict.fromkeys(r["trace_id"] for r in rows))
    out.update({"started": iso(rows[0]["start_ms"]) if rows else None,
                "last": iso(max(r["start_ms"] + (r["duration_ms"] or 0) for r in rows)) if rows else None,
                "requests": len(order), "errors": len({r["trace_id"] for r in rows if r["error"]}),
                "hosts": sorted({r["host"] for r in rows if r["host"]})[:6], "services": sorted({r["service"] for r in rows if r["service"]})[:6],
                "position": order.index(trace_id) + 1 if trace_id in order else None})
    return out


@router.get("/apm/exceptions")
def exceptions(minutes: int = 60, service: str | None = None, env: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """Error lines of the live window grouped by exception type and service."""
    from datetime import datetime, timedelta, timezone

    from ...exceptions import group
    minutes = max(5, min(int(minutes or 60), 1440))
    obs = svc.live.snapshot(env=env or None, since=datetime.now(timezone.utc) - timedelta(minutes=minutes))
    obs = [o for o in obs if (sc is None or sc.allows(o.host)) and (not service or o.service == service)]
    return {"minutes": minutes, "groups": group(obs, minutes)}
