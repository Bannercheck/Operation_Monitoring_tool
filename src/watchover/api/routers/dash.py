"""Built-in dashboards' own aggregates (watchover.dash): infrastructure over a range, recent alarms, lines per pool. Same range
(last / start / end) and scope (env / pool / host, narrowed to the user's team) as /live/range/*; answers are kept five seconds per
scope, so the cache never crosses teams."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ... import dash
from ..security import access, require, services, user_scope
from .live import _hx, _rng

router = APIRouter(prefix="/dash", tags=["dash"])
CACHE = dash.TTLCache(ttl=5.0, size=128)


def _key(*parts):
    return tuple(tuple(p) if isinstance(p, list) else p for p in parts)


def _rkey(last, start, end):
    return ("last", int(last or 0)) if not start else ("abs", start, end or "")


@router.get("/infra")
def infra(last: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, env: Optional[str] = None, host: Optional[str] = None,
          pool: Optional[str] = None, top: int = Query(10, ge=1, le=50), series_top: int = Query(6, ge=1, le=8),
          user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """CPU / memory / disk / GPU for the hosts in scope: fleet gauges with their levels, fleet average per bar, the top hosts' lines and
    the per-server tables (newest, min, avg, max) ranked by the newest reading."""
    s, e, _live = _rng(last, start, end)
    h = _hx(svc, sc, host, pool)
    thr = getattr(svc, "thresholds", None)

    def levels(hn, m, ev):
        p = thr.effective(hn, ev).get(m) if thr is not None else None
        if not p:
            lv = dash.fleet_levels(g).get(m, {})
            return lv.get("warn"), lv.get("crit")
        return p.get("warn"), p.get("crit")
    g = (thr.globals_fn() if thr is not None and thr.globals_fn else {}) or {}
    return CACHE.get(_key("infra", _rkey(last, start, end), env, h, top, series_top),
                     lambda: dash.infra(svc.live, s, e, env or None, h, top, series_top, levels if thr is not None else None, g))


@router.get("/alarms")
def alarms(last: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, env: Optional[str] = None, host: Optional[str] = None,
           pool: Optional[str] = None, limit: int = Query(50, ge=1, le=200), user=Depends(require("page.ops")), svc=Depends(services), acc=Depends(access)):
    """Recent alarms across threshold states (page.thr), anomalies (page.anom) and the alert log (sys.notify / act.alerts), each source
    read only with its permission and narrowed to the scope that permission covers; with env / pool / host, to those servers."""
    s, e, _live = _rng(last, start, end)

    def scope_of(perm):
        return acc.scope(perm) if acc.can(perm) else False

    sc_thr, sc_an = scope_of("page.thr"), scope_of("page.anom")
    al_ok = acc.can("sys.notify") or acc.can("act.alerts")
    narrow = None
    if env or host or pool:
        sc_ops = acc.scope("page.ops")
        h = _hx(svc, sc_ops, host, pool)
        hs = dash._hset(h)
        envs = {hn: (ev or "unknown") for hn, ev in svc.live.hosts().items()} if env else {}     # host -> environment
        narrow = lambda hn: bool(hn) and (hs is None or hn in hs) and (not env or envs.get(hn, "unknown") == env)  # noqa: E731
    mine = None if (not al_ok or acc.wide) else set(acc.team_ids("act.alerts")) | set(acc.team_ids("sys.notify"))
    key = _key("alarms", _rkey(last, start, end), env, host, pool, limit, acc.email, sc_thr is False, sc_an is False, al_ok)

    def build():
        allow = lambda sc, hn: sc is None or sc.allows(hn)  # noqa: E731
        states = [] if sc_thr is False else [r for r in svc.thresholds.state() if allow(sc_thr, r["host"]) and (narrow is None or narrow(r["host"]))]
        anoms = []
        if sc_an is not False:
            for r in svc.anomalies.list(None, env or None, None, 500):
                if sc_an is not None and not (sc_an.allows(r["host"]) or sc_an.allows_service(r.get("service") or "")):
                    continue
                if narrow is not None and (host or pool) and not narrow(r["host"]):
                    continue
                anoms.append(r)
        alerts = []
        if al_ok:
            for x in svc.notifier.alerts(1000):
                k = str(x.get("key") or "")
                hn = k.split(":")[1] if k.startswith("thr:") and k.count(":") >= 2 else ""
                if mine is not None and x.get("team_id") not in mine:      # as /notify/alerts: a team account reads its teams' alerts only
                    continue
                if narrow is not None and (host or pool or env) and not (hn and narrow(hn)):
                    continue
                alerts.append(x)
        return dash.alarms(s, e, states, anoms, alerts, limit)
    return CACHE.get(key, build)


@router.get("/pools")
def pools(last: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, env: Optional[str] = None, host: Optional[str] = None,
          pool: Optional[str] = None, top: int = Query(8, ge=1, le=12), user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    """Lines and ERROR+ per pool and bar (the stacked area per pool), the pools past `top` folded into one "~other" series."""
    s, e, _live = _rng(last, start, end)
    h = _hx(svc, sc, host, pool)
    return CACHE.get(_key("pools", _rkey(last, start, end), env, h, top), lambda: dash.pools(svc.ranges, s, e, env or None, h, top))
