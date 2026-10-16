"""Verification (docs/design/action-agent.md §6.2 verify steps, §10.6; C23, R7, R8).

A verify step names server-side checks and/or diagnose templates with an `expect`. Server-side checks read the dashboard's
own data (`Sources`): metrics, log signatures, anomalies, business flows, client SLOs and an HTTP probe from the server; a
check is **independent** when its data does not come from the acting host's own agent or actuator (`http_ok` probed from the
server or a peer, `slo_ok`, `log_*` on other hosts, `bizflow_stuck_below`). Template checks run through the actuator as
`verify` jobs (the engine dispatches them) or, without a confirmed actuator, become a guided paste box.

`require: all | any`; `retries` and `interval_s` (time-scaled in demos). A check without data answers `None`: when no check
produced data the act step is `unverified`, which blocks L3+ for that run. The same checks re-run after a rollback
(post-rollback verify, R8): only a passing one settles the run as `rolled_back`."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import conditions

SERVER_CHECKS = ("metric_below", "metric_above", "log_absent", "log_present", "http_ok", "anomaly_cleared", "bizflow_stuck_below", "slo_ok")


def _home() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data")


def demo_disk_file() -> Path:
    return _home() / "act_demo_disk.json"


class Sources:
    """What verify and watch read. Every method answers None when it has no data (never raises)."""

    def __init__(self, act):
        self.act = act

    @property
    def live(self):
        return getattr(getattr(self.act, "svc", None), "live", None)

    def metric(self, host: str, metric: str, within_s: int = 300) -> Optional[float]:
        from .policy import act_demo
        if act_demo() and demo_disk_file().exists():                 # demo: the simulated disk of `demo disk`
            try:
                d = json.loads(demo_disk_file().read_text(encoding="utf-8"))
                if metric in ("disk", "disk_pct") and host in d:
                    return float(d[host])
            except (ValueError, OSError):
                pass
        live = self.live
        if live is None:
            return None
        try:
            st = live.metric_stats(window_min=max(1, int(within_s // 60) or 1), host=host)
            vals = [v for (m, _h), v in (st.get("latest") or {}).items() if m == metric]
            return float(vals[-1]) if vals else None
        except Exception:  # noqa: BLE001
            return None

    def log_count(self, phrases: List[str], host: str, window_s: int, at: datetime) -> Optional[int]:
        live = self.live
        if live is None:
            return None
        try:
            obs = live.snapshot(host=host or None, since=at - timedelta(seconds=window_s))
        except Exception:  # noqa: BLE001
            return None
        low = [p.lower() for p in phrases if p]
        return sum(1 for o in obs if all(p in (o.message or "").lower() for p in low))

    def http_status(self, url: str, timeout: float = 5.0) -> Optional[int]:
        if not re.match(r"^https?://", url or ""):
            return None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:  # noqa: S310 - allowlisted url param
                return int(r.status)
        except urllib.error.HTTPError as e:
            return int(e.code)
        except Exception:  # noqa: BLE001
            return 0

    def anomaly_open(self, host: str, key: str) -> Optional[bool]:
        try:
            rows = self.act.db._exec("SELECT phase FROM actions WHERE kind='case' AND host=? AND case_key=? ORDER BY id DESC LIMIT 1", (host, key))
        except Exception:  # noqa: BLE001
            return None
        return None if not rows else rows[0]["phase"] not in ("resolved", "auto_resolved", "dismissed", "suppressed")

    def bizflow_stuck(self, flow: str, step: str) -> Optional[int]:
        bf = getattr(getattr(self.act, "svc", None), "bizflows", None)
        if bf is None:
            return None
        try:
            for s in bf.check_stuck(0) or []:
                if str(s.get("flow")) == flow and (not step or str(s.get("step")) == step):
                    return int(s.get("stuck") or s.get("count") or 0)
            return 0
        except Exception:  # noqa: BLE001
            return None

    def slo(self, host: str, window_min: int = 15, at: Optional[datetime] = None) -> Optional[float]:
        live = self.live
        if live is None:
            return None
        try:
            s = live.slo(window_min=window_min, host=host)
            v = s.get("availability") if isinstance(s, dict) else None
            return float(v) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    # ---- watch (§4.7)
    def errors(self, host: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        live = self.live
        if live is None:
            return None
        try:
            obs = [o for o in live.snapshot(host=host, since=start) if o.timestamp <= end]
        except Exception:  # noqa: BLE001
            return None
        err = [o for o in obs if str(o.severity).upper() in ("ERROR", "CRITICAL", "FATAL")]
        mins = max((end - start).total_seconds() / 60.0, 1e-6)
        return {"count": len(err), "per_min": len(err) / mins, "templates": sorted({o.template or o.fingerprint or o.message[:80] for o in err})}

    def templates_seen(self, host: str, start: datetime, end: datetime) -> Optional[set]:
        e = self.errors(host, start, end)
        return None if e is None else set(e["templates"])

    def new_cases(self, host: str, since: datetime, exclude_key: str) -> List[str]:
        try:
            rows = self.act.db._exec("SELECT case_key FROM actions WHERE kind='case' AND host=? AND first_seen>=? AND case_key<>?",
                                     (host, since.isoformat(timespec="seconds"), exclude_key or ""))
        except Exception:  # noqa: BLE001
            return []
        return [r["case_key"] for r in rows]

    def agent_last_seen(self, host: str) -> Optional[datetime]:
        live = self.live
        if live is None:
            return None
        try:
            ag = live.agents or {}
            best = None
            for name, ts in ag.items():
                if name == host:
                    best = datetime.fromtimestamp(float(ts), timezone.utc)
            return best
        except Exception:  # noqa: BLE001
            return None


def parse_query(q: str):
    """'"No space left on device" host:web-07' → (["No space left on device"], "web-07")."""
    phrases = re.findall(r'"([^"]+)"', q or "")
    rest = re.sub(r'"[^"]*"', " ", q or "")
    host = ""
    for tok in rest.split():
        if tok.startswith("host:"):
            host = tok[5:]
        elif tok:
            phrases.append(tok)
    return phrases, host


def server_check(kind: str, spec: Dict[str, Any], host: str, sources, at: datetime) -> Dict[str, Any]:
    """One server-side check: {"kind", "ok": True / False / None (no data), "independent", "value"}."""
    spec = spec or {}
    out: Dict[str, Any] = {"kind": kind, "ok": None, "independent": False, "value": None}
    if kind in ("metric_below", "metric_above"):
        v = sources.metric(host, str(spec.get("metric") or ""), int(spec.get("within_s") or 300))
        out["value"] = v
        if v is not None:
            lim = float(spec.get("value"))
            out["ok"] = v < lim if kind == "metric_below" else v > lim
    elif kind in ("log_absent", "log_present"):
        phrases, qhost = parse_query(str(spec.get("query") or ""))
        n = sources.log_count(phrases, qhost or host, int(spec.get("window_s") or 300), at)
        out["value"], out["independent"] = n, bool(qhost and qhost != host)
        if n is not None:
            out["ok"] = n == 0 if kind == "log_absent" else n > 0
    elif kind == "http_ok":
        url = str(spec.get("url") or "")
        if url:
            st = sources.http_status(url)
            out["value"] = st
            want = int(spec.get("expect_status") or 200)
            out["ok"] = None if st is None else st == want
            out["independent"] = str(spec.get("probe_from") or "server").startswith(("server", "peer:"))
    elif kind == "anomaly_cleared":
        o = sources.anomaly_open(host, str(spec.get("key") or ""))
        out["ok"] = None if o is None else not o
    elif kind == "bizflow_stuck_below":
        n = sources.bizflow_stuck(str(spec.get("flow") or ""), str(spec.get("step") or ""))
        out["value"], out["independent"] = n, True
        out["ok"] = None if n is None else n < int(spec.get("value") or 1)
    elif kind == "slo_ok":
        v = sources.slo(host, int(spec.get("window_min") or 15), at)
        out["value"], out["independent"] = v, True
        out["ok"] = None if v is None else v >= float(spec.get("min") or 99.0)
    return out


def check_kind(check: Dict[str, Any]) -> str:
    if check.get("template"):
        return "template"
    for k in SERVER_CHECKS:
        if k in check:
            return k
    return ""


def template_check(check: Dict[str, Any], fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A diagnose template's result against the check's `expect` (bare captures)."""
    out = {"kind": "template", "template": check.get("template"), "ok": None, "independent": False, "value": None}
    if fields is None:
        return out
    out["value"] = {k: v for k, v in fields.items() if not isinstance(v, (list, dict))}
    exp = check.get("expect")
    out["ok"] = bool(conditions.evaluate(exp, {"captures": fields})) if exp else True
    return out


def combine(results: List[Dict[str, Any]], require: str = "all") -> Dict[str, Any]:
    """{"ok": True / False / None, "no_data": bool, "independent_ok": bool, "checks": results}."""
    have = [r for r in results if r.get("ok") is not None]
    if not have:
        return {"ok": None, "no_data": True, "independent_ok": False, "checks": results}
    ok = all(r["ok"] for r in have) if require != "any" else any(r["ok"] for r in have)
    if require != "any" and any(r.get("ok") is None for r in results):
        ok = ok and bool(have)                                    # missing data never turns a failure into a pass
    return {"ok": bool(ok), "no_data": False, "independent_ok": any(r.get("independent") and r.get("ok") for r in have), "checks": results}


def render(value, values: Dict[str, Any]):
    """'{path}' / '{inventory.health_url}' / '{host}' in check specs (strings only)."""
    if isinstance(value, dict):
        return {k: render(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, values) for v in value]
    if not isinstance(value, str):
        return value

    def rep(m):
        v = values.get(m.group(1))
        return "" if v is None else str(v.get("pid") if isinstance(v, dict) and "pid" in v else v)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", rep, value)


__all__ = ["Sources", "server_check", "template_check", "combine", "check_kind", "render", "parse_query", "demo_disk_file"]
