"""Live feed: per-minute stats, infra metrics, SLO, hosts / environments, recent events, SLO history, simulator, buffer -> dataset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ... import report as wo_report
from ..security import require, require_install, services, user_scope

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


def _hp(svc, sc, host: str | None, pool: str | None):
    """Host filter with an optional pool (one name or a comma list): the pool's hosts, narrowed by host and by the team."""
    if not pool:
        return _h(sc, host)
    want = {p.strip() for p in pool.split(",") if p.strip()}
    r = svc.live.rollup
    hs = tuple(sorted(h for h in list(r.hosts) if r.pool_of(h) in want))
    asked = host_(host)
    if asked is not None:
        asked = (asked,) if isinstance(asked, str) else asked
        hs = tuple(h for h in hs if h in asked)
    hs = hs or ("\x00no-host",)
    return _h(sc, hs[0] if len(hs) == 1 else ",".join(hs)) if sc is not None else (hs[0] if len(hs) == 1 else hs)


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
def stats(window: int = 15, env: str | None = None, host: str | None = None, pool: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    s = svc.live.stats(window, env or None, _hp(svc, sc, host, pool))
    s["rows"] = _iso(s.get("rows", []))
    if sc is not None and isinstance(s.get("agents"), dict):     # the fleet roster: a team sees only its own agents (as on /live/hosts)
        s["agents"] = {k: v for k, v in s["agents"].items() if sc.allows(k)}
    return s


@router.get("/metrics")
def metrics(window: int = 15, env: str | None = None, host: str | None = None, pool: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    m = svc.live.metric_stats(window, env or None, _hp(svc, sc, host, pool))
    m["per_minute"] = _iso(m.get("per_minute", []))
    m["latest"] = [{"metric": k[0], "host": k[1], "value": v} for k, v in m.get("latest", {}).items()]
    return m


@router.get("/slo")
def slo(window: int = 15, env: str | None = None, host: str | None = None, pool: str | None = None, detail: bool = False, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.live.slo_detail(window, env or None, _hp(svc, sc, host, pool)) if detail else svc.live.slo(window, env or None, _hp(svc, sc, host, pool))


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
def events(n: int = Query(50, le=2000), env: str | None = None, host: str | None = None, pool: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return [{"timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host, "environment": o.environment,
             "message": o.message, "source": o.source, "agent": o.attributes.get("agent", "")} for o in svc.live.tail(n, env or None, _hp(svc, sc, host, pool))]


@router.get("/files")
def files(window: int = 15, env: str | None = None, host: str | None = None, pool: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.live.files(window, env or None, _hp(svc, sc, host, pool))


@router.get("/history")
def history(hours: int = 24, env: str | None = None, host: str | None = None, pool: str | None = None, bucket: str = Query("hour", pattern="^(hour|day)$"),
            user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """SLO figures from the minute rollups (weekly / monthly reports use the same call)."""
    end = datetime.now(UTC)
    fig = svc.history.figures(end - timedelta(hours=hours), end, env or None, _hp(svc, sc, host, pool), bucket)
    fig["coverage"] = svc.history.coverage()
    return fig


class SimIn(BaseModel):
    on: bool


@router.post("/simulate")
def simulate(body: SimIn, user=Depends(require_install("act.sim")), svc=Depends(services)):    # installation-wide: fake hosts for everyone
    return {"simulator": svc.simulate(body.on)}


@router.post("/analyze", status_code=202)
def analyze(team_id: int | None = None, user=Depends(require("page.data")), svc=Depends(services), sc=Depends(user_scope)):
    """Snapshot the live buffer into a dataset load job. A team-scoped account snapshots only its own hosts' lines, and the
    dataset belongs to its team."""
    from ... import ownership
    from ..security import pick_team
    if not svc.live.received:
        raise ValueError("the live buffer is empty")
    name, data = svc.live.to_dataset()
    acc = svc.access_of(user)
    if sc is not None:
        data = ownership.scoped_jsonl(data, sc.allows)
        if not data:
            raise ValueError("the live buffer holds no line of your team's hosts")
    tid = pick_team(acc, "page.data", team_id) if not acc.wide else None
    return ownership.submit(svc, tid, name, data)


@router.post("/clear")
def clear(user=Depends(require_install("act.sim")), svc=Depends(services)):    # empties every team's live buffer: installation-wide
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
def slo_report(window: int = 60, env: str | None = None, host: str | None = None, pool: str | None = None, lang: str = "tr", dataset: str | None = None,
               start: str | None = None, end: str | None = None,
               user=Depends(require("act.report")), svc=Depends(services), sc=Depends(user_scope)):
    """The SLO report (availability, error budget, p95, worst services) as a self-contained HTML page; windows above a day come from the rollups.
    start / end: a fixed range chosen on the operations screen (from the per-minute history)."""
    from pathlib import Path
    if start:
        from ...timerange import parse_range
        s_, e_, _ = parse_range(None, start, end)
        mins = (e_ - s_).total_seconds() / 60
        fig = svc.history.figures(s_, e_, env or None, _hp(svc, sc, host, pool), "minute" if mins <= 360 else "hour" if mins <= 7 * 1440 else "day")
    elif window <= 1440:
        fig = wo_report.figures_live(svc.live, env or None, _hp(svc, sc, host, pool), int(window))
    else:
        now = datetime.now(UTC)
        fig = svc.history.figures(now - timedelta(minutes=int(window)), now, env or None, _hp(svc, sc, host, pool), "day" if window > 7 * 1440 else "hour")
    a = svc.datasets.get(dataset)["analysis"] if dataset else None
    scope = " · ".join(x for x in (env, host) if x) or ("all" if lang == "en" else "tümü")
    logo = Path(__file__).resolve().parents[4] / "assets" / "logo.svg"
    html = wo_report.slo_report(fig, a, scope, "", lang, wo_settings_workspace(), logo.read_text(encoding="utf-8") if logo.exists() else "")
    return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="watchover-slo-{datetime.now(UTC).strftime("%Y%m%d-%H%M")}.html"'})


def wo_settings_workspace() -> str:
    from ... import settings as wo_settings
    return wo_settings.load().get("workspace", "") or ""


@router.get("/pools")
def pools(window: int = 15, env: str | None = None, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Server pools (inventory cluster, else the host name without its number) with every host's volume, errors and silence."""
    window = max(1, min(int(window or 15), 60))
    allow = None if sc is None else sc.allows
    rows = svc.live.rollup.pools(window, env or None, allow)
    return {"window": window, "pools": rows, "hosts": sum(p["host_count"] for p in rows), "silent": sum(p["silent"] for p in rows)}


@router.get("/groups")
def groups(window: int = 15, env: str | None = None, host: str | None = None, pool: str | None = None, severity: str = "INFO", q: str = "", service: str = "",
           limit: int = Query(100, le=500), user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Deduplicated logs: one row per message template and service, with count, hosts, pools and trend."""
    window = max(1, min(int(window or 15), 60))
    h = _hp(svc, sc, host, pool)
    if isinstance(h, str) and "," in h:
        h = tuple(h.split(","))
    return svc.live.rollup.groups(window, env or None, h, severity or "INFO", q or "", service or "", limit)


# ---------------------------------------------------------------- any time range (the operations screen's time picker)
def _rng(last, start, end):
    from ...timerange import parse_range
    return parse_range(last, start, end)


def _hx(svc, sc, host, pool):
    h = _hp(svc, sc, host, pool)
    if isinstance(h, str) and "," in h:
        h = tuple(h.split(","))
    return h


@router.get("/range/summary")
def range_summary(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                  user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Timeline and headline numbers (with the previous range for deltas) for `last` minutes or an absolute start / end."""
    s, e, live = _rng(last, start, end)
    out = svc.ranges.summary(s, e, env or None, _hx(svc, sc, host, pool))
    return {**out, "live": live}


@router.get("/range/fleet")
def range_fleet(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None,
                user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    s, e, _ = _rng(last, start, end)
    return svc.ranges.fleet(s, e, env or None, None if sc is None else sc.allows)


@router.get("/range/groups")
def range_groups(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                 severity: str = "INFO", q: str = "", service: str = "", limit: int = Query(100, le=500), user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    s, e, _ = _rng(last, start, end)
    return svc.ranges.groups(s, e, env or None, _hx(svc, sc, host, pool), severity or "INFO", q or "", service or "", limit)


@router.get("/range/logs")
def range_logs(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
               severity: str = "INFO", q: str = "", service: str = "", limit: int = Query(200, le=1000), before: str | None = None,
               user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    s, e, _ = _rng(last, start, end)
    return svc.ranges.logs(s, e, env or None, _hx(svc, sc, host, pool), severity or "INFO", q or "", service or "", limit, before)


@router.get("/range/detail")
def range_detail(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                 user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """What is behind the headline numbers for the range: errors by service / host, availability per bar and budget burn, the groups
    that lower it, latency per service and the slowest lines, the services table."""
    s, e, live = _rng(last, start, end)
    return {**svc.ranges.detail(s, e, env or None, _hx(svc, sc, host, pool)), "live": live}


@router.get("/range/sessions")
def range_sessions(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                   q: str = "", limit: int = Query(200, ge=1, le=1000), user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Client sessions in the range (pseudonymised ids, users and addresses only): distinct total, active per bar, per host, the list.
    The web / mobile figures of the user sensors are not tied to a server: left out (null) once hosts, a pool or a team narrow the scope."""
    s, e, _ = _rng(last, start, end)
    h = _hx(svc, sc, host, pool)
    return svc.ranges.sessions(s, e, env or None, h, q or "", limit, getattr(svc, "rum", None) if h is None else None)


@router.get("/range/latency")
def range_latency(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                  user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """p50 / p90 / p99 per bar (ms; null where a bar has no duration sample)."""
    s, e, _ = _rng(last, start, end)
    return svc.ranges.latency(s, e, env or None, _hx(svc, sc, host, pool))


@router.get("/range/hostseries")
def range_hostseries(last: int | None = None, start: str | None = None, end: str | None = None, env: str | None = None, host: str | None = None, pool: str | None = None,
                     metric: str = Query("lines", pattern="^(lines|errors|error_rate|p95|sessions)$"), top: int = Query(6, ge=1, le=20),
                     user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """The top hosts in scope by one metric over the range and their value per bar (one line per host on the chart)."""
    s, e, _ = _rng(last, start, end)
    return svc.ranges.hostseries(s, e, env or None, _hx(svc, sc, host, pool), metric, top)
