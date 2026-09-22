"""ITSM: tickets from ServiceNow / Jira / OneDesk / a generic REST endpoint (or the demo set), correlated with live evidence and incidents."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import itsm as wo_itsm
from ... import settings as wo_settings
from ...analysis import template_of
from ...models import SEV_RANK
from ..security import require, services

router = APIRouter(prefix="/itsm", tags=["itsm"])
UTC = timezone.utc
KEYS = ("x_itsm_system", "x_itsm_base", "x_itsm_user", "x_itsm_pass", "x_itsm_token", "x_itsm_query", "x_itsm_path", "x_itsm_mapping")
SYSTEMS = {"servicenow": wo_itsm.fetch_servicenow, "jira": wo_itsm.fetch_jira, "onedesk": wo_itsm.fetch_onedesk}
MASK = "•••"
_cache: dict = {"tickets": None, "at": 0.0, "system": ""}


class ConfigIn(BaseModel):
    system: str | None = None
    base: str | None = None
    user: str | None = None
    password: str | None = None
    token: str | None = None
    query: str | None = None
    path: str | None = None
    mapping: str | None = None


def _cfg() -> dict:
    c = wo_settings.load()
    return {"system": c.get("x_itsm_system") or "demo", "base": c.get("x_itsm_base", ""), "user": c.get("x_itsm_user", ""), "password": c.get("x_itsm_pass", ""),
            "token": c.get("x_itsm_token", ""), "query": c.get("x_itsm_query", ""), "path": c.get("x_itsm_path", ""), "mapping": c.get("x_itsm_mapping", "")}


def live_signals(ls, window_min: int = 15) -> list[dict]:
    start = datetime.now(UTC) - timedelta(minutes=window_min)
    groups: dict[str, dict] = {}
    for o in ls.snapshot():
        if o.timestamp < start or SEV_RANK[o.severity] < 3:
            continue
        tpl = template_of(o.message)
        g = groups.setdefault(tpl, {"id": f"L{len(groups) + 1}", "template": tpl, "services": set(), "hosts": set(), "severity": o.severity, "onset": o.timestamp, "count": 0})
        g["count"] += 1
        if o.service:
            g["services"].add(o.service)
        if o.host:
            g["hosts"].add(o.host)
        g["onset"] = min(g["onset"], o.timestamp)
    out = [dict(g, services=sorted(g["services"]), hosts=sorted(g["hosts"])) for g in groups.values() if g["count"] >= 3]
    return sorted(out, key=lambda g: -g["count"])[:12]


def fetch(c: dict) -> list:
    if c["system"] == "demo":
        return wo_itsm.demo_tickets()
    if c["system"] == "generic":
        headers = {"Authorization": f"Bearer {c['token']}"} if c["token"] else {}
        return wo_itsm.fetch_generic(c["base"], headers, json.loads(c["mapping"] or "{}"), c["path"])
    kw = {"user": c["user"], "password": c["password"], "token": c["token"]}
    if c["query"]:
        kw["query"] = c["query"]
    return SYSTEMS[c["system"]](c["base"], **kw)


def _dict(x) -> dict:
    return {"id": x.id, "title": x.title, "description": x.description, "priority": x.priority, "status": x.status, "created": x.created.isoformat() if x.created else "",
            "service": x.service, "assignee": x.assignee, "url": x.url, "relevance": x.relevance, "related": x.related, "reasons": x.reasons}


@router.get("/config")
def get_config(user=Depends(require("page.itsm"))):
    c = _cfg()
    return {**c, "password": MASK if c["password"] else "", "token": MASK if c["token"] else "", "systems": ["demo", *SYSTEMS, "generic"]}


@router.put("/config")
def put_config(body: ConfigIn, user=Depends(require("act.itsm"))):
    m = {"system": "x_itsm_system", "base": "x_itsm_base", "user": "x_itsm_user", "password": "x_itsm_pass", "token": "x_itsm_token", "query": "x_itsm_query", "path": "x_itsm_path", "mapping": "x_itsm_mapping"}
    vals = {m[k]: v for k, v in body.model_dump().items() if v is not None and v != MASK}
    cur = _cfg()
    new_base = body.model_dump().get("base")
    if new_base is not None and new_base != cur.get("base") and body.password is None and body.token is None:
        vals["x_itsm_pass"] = ""; vals["x_itsm_token"] = ""     # never send the stored credentials to a newly pointed endpoint
    wo_settings.save(vals)
    _cache.update({"tickets": None, "system": ""})
    return {"ok": True}


@router.get("/tickets")
def tickets(dataset: str | None = None, refresh: bool = False, user=Depends(require("page.itsm")), svc=Depends(services)):
    if refresh and not svc.roles.can(user["role"], "act.itsm"):
        refresh = False                                       # only integration managers trigger a live fetch to the configured endpoint
    """Tickets scored against the last 15 minutes of live signals, metric breaches and (optionally) a dataset's incidents."""
    import time
    c = _cfg()
    if refresh or _cache["tickets"] is None or _cache["system"] != c["system"] or time.time() - _cache["at"] > 300:
        _cache.update({"tickets": fetch(c), "at": time.time(), "system": c["system"]})
    a = svc.datasets.get(dataset)["analysis"] if dataset else None
    incs = [{"id": i.id, "title": i.title, "services": i.affected_services, "hosts": i.affected_hosts, "started_at": i.started_at} for i in a.incidents] if a else []
    breaches = svc.live.metric_stats(15).get("breaches", [])
    out = wo_itsm.correlate(_cache["tickets"], live_signals(svc.live), breaches, incs)
    return {"system": c["system"], "fetched_at": _cache["at"], "tickets": [_dict(x) for x in out]}
