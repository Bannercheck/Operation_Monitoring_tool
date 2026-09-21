"""Failure map: services as nodes, dependency errors and correlations as edges, root causes flagged; built for a dataset or the live buffer."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...graph import build_map, to_html
from ...i18n import t as _tr
from ..security import require, services

router = APIRouter(prefix="/map", tags=["map"])
SEV_COLORS = {"CRITICAL": "#f87171", "ERROR": "#fb923c", "WARN": "#fbbf24", "INFO": "#60a5fa", "DEBUG": "#64748b"}


def _labels(lang: str) -> dict:
    keys = {"root": "map_l_root_tag", "dep": "map_dep_lbl", "hosts": "env_hosts", "signals": "signals_n", "depends_on": "map_depends_on", "depended_by": "map_depended_by",
            "on_hosts": "ops_hosts", "reset": "map_reset", "hint": "map_hint", "kind_root": "map_l_root", "kind_affected": "map_l_affected", "kind_erroring": "map_l_erroring", "kind_clean": "map_l_clean"}
    out = {k: _tr(v, lang).rstrip(":") for k, v in keys.items()}   # t(key, lang)
    out["errors"] = "ERROR+"
    return out


@router.get("")
def failure_map(dataset: str = Query("live", description="dataset id or 'live'"), incident: str | None = None, env: str | None = None, host: str | None = None,
                hosts: bool = True, lang: str = "tr", user=Depends(require("page.map")), svc=Depends(services)):
    a = svc.live_analysis() if dataset == "live" else svc.datasets.get(dataset)["analysis"]
    if a is None or not a.observations:
        return {"empty": True, "nodes": 0}
    m = build_map(a, incident or None, env=env or None, host=host or None, with_hosts=hosts)
    sig_by_svc: dict[str, list[dict]] = {}
    for s_ in a.signals:
        for svc_ in s_.services:
            if any(n["id"] == svc_ for n in m["nodes"]) and len(sig_by_svc.setdefault(svc_, [])) < 6:
                sig_by_svc[svc_].append({"severity": s_.severity, "count": s_.count, "template": s_.template[:110], "hosts": ", ".join(sorted(s_.hosts)[:3]), "color": SEV_COLORS.get(s_.severity, "#8b98ad")})
    html = to_html(m, _labels(lang), sig_by_svc, height=640) if m["nodes"] else ""
    return {"empty": not m["nodes"], "html": html, "nodes": len(m["nodes"]), "deps": len(m["deps"]), "dep_events": sum(e["n"] for e in m["deps"]), "corr": len(m["corr"]),
            "root": m["root"], "incidents_hit": m["incidents"], "errors_total": m["errors_total"], "affected": len(m["affected"]), "hosts": len({h["host"] for h in m["hosts"]}),
            "incidents": [{"id": i.id, "title": i.title[:60]} for i in a.incidents], "envs": sorted({o.environment or "unknown" for o in a.observations}),
            "host_list": sorted({o.host for o in a.observations if o.host})[:300]}
