"""Live feed: per-minute stats, infra metrics, SLO, hosts / environments, recent events, SLO history, simulator, buffer -> dataset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ... import report as wo_report
from ..security import require, services, user_scope

router = APIRouter(prefix="/live", tags=["live"])
UTC = timezone.utc


def host_(h: str | None):
    """One name, or a comma list -> tuple (the store treats a tuple as "these hosts together")."""
    if not h:
        return None
    parts = [x.strip() for x in h.split(",") if x.strip()]
    return parts[0] if len(parts) == 1 else tuple(parts)


def _h(sc, h: str | None):
    """The host filter for the live store: what was asked, narrowed to the user's team hosts."""
    return host_(h) if sc is None else sc.host_filter(host_(h))


def _accepts_host(svc) -> bool:
    import inspect
    return "host" in inspect.signature(svc.live.env_summary).parameters


def _iso(rows):
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return out


@router.get("/stats")
def stats(window: int = 15, env: str | None = None, host: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    s = svc.live.stats(window, env or None, _h(sc, host))
    s["rows"] = _iso(s.get("rows", []))
    return s


@router.get("/metrics")
def metrics(window: int = 15, env: str | None = None, host: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    m = svc.live.metric_stats(window, env or None, _h(sc, host))
    m["per_minute"] = _iso(m.get("per_minute", []))
    m["latest"] = [{"metric": k[0], "host": k[1], "value": v} for k, v in m.get("latest", {}).items()]
    return m


@router.get("/slo")
def slo(window: int = 15, env: str | None = None, host: str | None = None, detail: bool = False, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.live.slo_detail(window, env or None, _h(sc, host)) if detail else svc.live.slo(window, env or None, _h(sc, host))


@router.get("/hosts")
def hosts(user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    if sc is None:
        return {"hosts": svc.live.hosts(), "environments": svc.live.environments(), "agents": sorted(svc.live.agents), "summary": svc.live.env_summary(15)}
    hs = {h: e for h, e in svc.live.hosts().items() if sc.allows(h)}
    envs: dict[str, int] = {}
    for o in svc.live.snapshot():
        if sc.allows(o.host):
            envs[o.environment or "unknown"] = envs.get(o.environment or "unknown", 0) + 1
    return {"hosts": hs, "environments": envs, "agents": sorted(a for a in svc.live.agents if sc.allows(a)), "summary": [],
            "teams": sc.teams}


@router.get("/events")
def events(n: int = Query(50, le=2000), env: str | None = None, host: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return [{"timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host, "environment": o.environment,
             "message": o.message, "source": o.source, "agent": o.attributes.get("agent", "")} for o in svc.live.tail(n, env or None, _h(sc, host))]


@router.get("/files")
def files(window: int = 15, env: str | None = None, host: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.live.files(window, env or None, _h(sc, host))


@router.get("/history")
def history(hours: int = 24, env: str | None = None, host: str | None = None, bucket: str = Query("hour", pattern="^(hour|day)$"),
            user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """SLO figures from the minute rollups (weekly / monthly reports use the same call)."""
    end = datetime.now(UTC)
    fig = svc.history.figures(end - timedelta(hours=hours), end, env or None, _h(sc, host), bucket)
    fig["coverage"] = svc.history.coverage()
    return fig


class SimIn(BaseModel):
    on: bool


@router.post("/simulate")
def simulate(body: SimIn, user=Depends(require("act.sim")), svc=Depends(services)):
    return {"simulator": svc.simulate(body.on)}


@router.post("/analyze", status_code=202)
def analyze(user=Depends(require("page.data")), svc=Depends(services)):
    """Snapshot the live buffer into a dataset load job."""
    if not svc.live.received:
        raise ValueError("the live buffer is empty")
    name, data = svc.live.to_dataset()
    return svc.datasets.submit(name, data)


@router.post("/clear")
def clear(user=Depends(require("act.sim")), svc=Depends(services)):
    svc.live.clear()
    return {"ok": True}


@router.get("/discoveries")
def discoveries(user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Applications and files each agent reported at start-up (--auto discovery)."""
    return {a: [{"app": x["app"], "files": x["files"], "ts": x["ts"].isoformat() if hasattr(x["ts"], "isoformat") else str(x["ts"])} for x in v] for a, v in svc.live.discoveries().items() if sc is None or sc.allows(a)}


@router.get("/lines")
def lines(agent: str, source: str, n: int = Query(400, le=5000), errors: bool = False, host: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Tail of one discovered log file as the agent sent it."""
    return [{"timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host, "message": o.message, "line_no": o.line_no}
            for o in svc.live.file_lines(agent, source, n, errors, _h(sc, host))]


@router.get("/report", response_class=HTMLResponse)
def slo_report(window: int = 60, env: str | None = None, host: str | None = None, lang: str = "tr", dataset: str | None = None,
               user=Depends(require("act.report")), svc=Depends(services), sc=Depends(user_scope)):
    """The SLO report (availability, error budget, p95, worst services) as a self-contained HTML page; windows above a day come from the rollups."""
    from pathlib import Path
    if window <= 1440:
        fig = wo_report.figures_live(svc.live, env or None, _h(sc, host), int(window))
    else:
        now = datetime.now(UTC)
        fig = svc.history.figures(now - timedelta(minutes=int(window)), now, env or None, _h(sc, host), "day" if window > 7 * 1440 else "hour")
    a = svc.datasets.get(dataset)["analysis"] if dataset else None
    scope = " · ".join(x for x in (env, host) if x) or ("all" if lang == "en" else "tümü")
    logo = Path(__file__).resolve().parents[4] / "assets" / "logo.svg"
    html = wo_report.slo_report(fig, a, scope, "", lang, wo_settings_workspace(), logo.read_text(encoding="utf-8") if logo.exists() else "")
    return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="watchover-slo-{datetime.now(UTC).strftime("%Y%m%d-%H%M")}.html"'})


def wo_settings_workspace() -> str:
    from ... import settings as wo_settings
    return wo_settings.load().get("workspace", "") or ""
