"""Watchover as an MCP server for the live product ("data giving"): the same answers the interface shows, as MCP tools.

Served by the API at POST /mcp (and /api/mcp) over streamable HTTP (JSON responses, stateless: no session to hijack, every
request carries its key). mcp_server.py (port 8765) keeps the dataset tools and forwards these when WATCHOVER_API_URL is set.

Each tool runs the interface's own endpoint function with the key's Access, so a team-bound key gets exactly the team view
(hosts, service rules, team-owned records) and a user-bound key exactly that user's view. Every answer goes through
privacy.scrub (KVKK: e-mail, phone, TCKN, IBAN, card, secrets masked) and a size cap; every call is audited (mcpkeys).
Write tools (create_action, ack_anomaly, set_threshold) need a key with the write permission and the underlying permission.

The registry (TOOLS) imports no FastAPI at module level, so mcp_server.py can read the catalogue without the API stack."""
from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import __version__

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_OUT = 200_000                     # characters of JSON per answer; longer lists are cut and marked "truncated"
INSTRUCTIONS = ("Watchover: operational noise -> signals -> explained incidents -> tracked actions. Read tools cover logs (query "
                "language, time range), dashboards, host detail, thresholds, anomalies, integrations, traces, business flows, inventory "
                "and actions, limited to this key's team / account scope. Personal data is masked. Start with whoami.")


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Tool:
    name: str
    perm: str                         # permission the key needs ("" = any key)
    write: bool
    tr: str                           # one-line label for the interface (Turkish / English)
    en: str
    description: str                  # for MCP clients
    props: dict                       # JSON Schema properties
    required: tuple
    fn: Callable

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.props, "required": list(self.required), "additionalProperties": False},
                "annotations": {"title": self.en, "readOnlyHint": not self.write, "destructiveHint": False, "idempotentHint": not self.write,
                                "openWorldHint": False}}

    def catalog(self) -> dict:
        return {"name": self.name, "perm": self.perm, "write": self.write, "tr": self.tr, "en": self.en}


def P(t: str, d: str, **kw) -> dict:
    return {"type": t, "description": d, **kw}


RANGE = {"last": P("integer", "Minutes back from now (default 60)", minimum=1, maximum=576000),
         "start": P("string", "Absolute range start, ISO 8601 (overrides last)"), "end": P("string", "Absolute range end, ISO 8601")}
WHERE = {"env": P("string", "Environment (prod, test, ...)"), "host": P("string", "Host name (or comma separated names)"), "pool": P("string", "Host pool")}


# ---------------------------------------------------------------- calling the interface's endpoint functions with a key's Access
def _route(fn, perm: str, ident, svc, **given):
    """Run a FastAPI endpoint function directly: dependencies filled from the key (user, Access, scope of `perm`, services),
    query defaults from the signature; HTTP errors become ToolErrors."""
    from fastapi import HTTPException
    from fastapi import params as fp
    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined
    acc = ident.acc
    if perm and not acc.can(perm):
        raise ToolError("forbidden", f"this key lacks the permission {perm}")
    sc = acc.scope(perm or None)
    req = SimpleNamespace(state=SimpleNamespace(wo_access=acc, wo_perm=perm), app=SimpleNamespace(state=SimpleNamespace(services=svc)),
                          headers={}, url=SimpleNamespace(path="/mcp"))
    deps = {"svc": svc, "sc": sc, "acc": acc, "request": req, "user": ident.user, "_u": ident.user}
    kw = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name in given:
            kw[name] = given[name]
        elif isinstance(p.default, fp.Depends):
            kw[name] = deps.get(name)
        elif isinstance(p.default, FieldInfo):
            kw[name] = None if p.default.default is PydanticUndefined else p.default.default
        elif p.default is inspect.Parameter.empty:
            if name not in deps:
                raise ToolError("bad_args", f"missing argument: {name}")
            kw[name] = deps[name]
        else:
            kw[name] = p.default
    try:
        return fn(**kw)
    except HTTPException as e:
        d = e.detail
        msg = d.get("en") or d.get("code") or json.dumps(d) if isinstance(d, dict) else str(d)
        code = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict", 429: "too_fast", 503: "busy"}.get(e.status_code, "error")
        raise ToolError(code, str(msg)[:300])
    except KeyError as e:
        raise ToolError("not_found", f"not found: {e.args[0] if e.args else ''}"[:200])
    except ValueError as e:
        raise ToolError("bad_request", str(e)[:300])


def _rng(a: dict) -> dict:
    start = a.get("start") or None
    return {"last": None if start else int(a.get("last") or 60), "start": start, "end": a.get("end") or None}


def _clamp(v, lo: int, hi: int, d: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def _host_ok(ident, perm: str, host: str) -> None:
    sc = ident.acc.scope(perm)
    if sc is not None and not sc.allows(host):
        raise ToolError("not_found", f"host not found: {host}")


# ---------------------------------------------------------------- tools
def whoami(svc, ident, **_):
    acc = ident.acc
    return {"key": ident.name, "principal": ident.principal, "scope": ident.scope, "write": ident.write,
            "teams": [m.team for m in acc.memberships], "all_hosts": acc.wide,
            "tools": [t.name for t in visible(ident)]}


def log_search(svc, ident, **a):
    from .api.routers import logs as R
    return _route(R.search, "page.logs", ident, svc, q=a.get("q") or "", **_rng(a), limit=_clamp(a.get("limit"), 1, 200, 50),
                  cursor=a.get("cursor") or None, cols="", x_tab_id=f"mcp-{ident.key_id or 0}")


def log_tail(svc, ident, **a):
    from .api.routers import logs as R
    return _route(R.tail, "page.logs", ident, svc, q=a.get("q") or "", after=a.get("after"), limit=_clamp(a.get("limit"), 1, 100, 30), cols="",
                  x_tab_id=f"mcp-tail-{ident.key_id or 0}")


def dash_infra(svc, ident, **a):
    from .api.routers import dash as R
    return _route(R.infra, "page.ops", ident, svc, **_rng(a), env=a.get("env"), host=a.get("host"), pool=a.get("pool"), top=_clamp(a.get("top"), 1, 50, 10), series_top=6)


def dash_alarms(svc, ident, **a):
    from .api.routers import dash as R
    return _route(R.alarms, "page.ops", ident, svc, **_rng(a), env=a.get("env"), host=a.get("host"), pool=a.get("pool"), limit=_clamp(a.get("limit"), 1, 200, 50))


def dash_pools(svc, ident, **a):
    from .api.routers import dash as R
    return _route(R.pools, "page.ops", ident, svc, **_rng(a), env=a.get("env"), host=a.get("host"), pool=a.get("pool"), top=_clamp(a.get("top"), 1, 12, 8))


def host_detail(svc, ident, **a):
    """Summary over the range, current metric values with their warn / crit levels (and where they come from), open breaches,
    recent alarms and the inventory record of one host."""
    from .api.routers import dash as D, live as L, thresholds as T, inventory as I
    host = str(a.get("host") or "").strip()
    if not host:
        raise ToolError("bad_args", "host is required")
    _host_ok(ident, "page.ops", host)
    out: dict = {"host": host, "summary": _route(L.range_summary, "page.ops", ident, svc, **_rng(a), env=None, host=host, pool=None)}
    out["alarms"] = _route(D.alarms, "page.ops", ident, svc, **_rng(a), env=None, host=host, pool=None, limit=20)
    if ident.acc.can("page.thr"):
        ov = _route(T.overview, "page.thr", ident, svc)
        out["metrics"] = next((r for r in ov.get("hosts", []) if r.get("host") == host), None)
        out["breaches"] = [s for s in _route(T.state, "page.thr", ident, svc) if s.get("host") == host]
    if ident.acc.can("page.inv"):
        try:
            out["inventory"] = _route(I.get, "page.inv", ident, svc, hostname=host)
        except ToolError:
            out["inventory"] = None
    return out


def thresholds_state(svc, ident, **a):
    from .api.routers import thresholds as T
    host = a.get("host") or None
    out = {"breaches": [s for s in _route(T.state, "page.thr", ident, svc) if not host or s.get("host") == host]}
    if a.get("overview") or host:
        ov = _route(T.overview, "page.thr", ident, svc)
        out["hosts"] = [r for r in ov.get("hosts", []) if not host or r.get("host") == host]
        out["last_run"] = ov.get("last_run")
    return out


def list_anomalies(svc, ident, **a):
    from .api.routers import anomalies as R
    return _route(R.list_anomalies, "page.anom", ident, svc, status=a.get("status") or "active", env=a.get("env"), kind=a.get("kind"),
                  limit=_clamp(a.get("limit"), 1, 500, 50))


def get_anomaly(svc, ident, **a):
    from .api.routers import anomalies as R
    return _route(R.get, "page.anom", ident, svc, aid=int(a["id"]))


def integration_hosts(svc, ident, **a):
    from .api.routers import apm as R
    return _route(R.integration_hosts, "page.apm", ident, svc, minutes=_clamp(a.get("minutes"), 1, 43200, 1440), env=a.get("env"))


def host_integrations(svc, ident, **a):
    from .api.routers import apm as R
    return _route(R.host_integrations, "page.apm", ident, svc, host=str(a["host"]), minutes=_clamp(a.get("minutes"), 1, 43200, 1440))


def search_traces(svc, ident, **a):
    from .api.routers import apm as R
    return _route(R.trace_search, "page.apm", ident, svc, service=a.get("service"), operation=a.get("operation"), minutes=_clamp(a.get("minutes"), 1, 43200, 60),
                  env=a.get("env"), min_ms=a.get("min_ms"), errors=bool(a.get("errors")), q=a.get("q"), session=a.get("session"), user_ref=None,
                  order=a.get("order") or "recent", limit=_clamp(a.get("limit"), 1, 100, 20), dataset=None)


def get_trace(svc, ident, **a):
    from .api.routers import apm as R
    return _route(R.trace_detail, "page.apm", ident, svc, trace_id=str(a["trace_id"]), dataset=None)


def trace_session(svc, ident, **a):
    from .api.routers import apm as R
    return _route(R.trace_session, "page.apm", ident, svc, trace_id=str(a["trace_id"]), dataset=None)


def list_flows(svc, ident, **a):
    from .api.routers import bizflow as R
    return _route(R.list_flows, "page.apm", ident, svc, minutes=_clamp(a.get("minutes"), 1, 43200, 1440))


def flow_funnel(svc, ident, **a):
    from .api.routers import bizflow as R
    return _route(R.funnel, "page.apm", ident, svc, fid=int(a["flow_id"]), minutes=_clamp(a.get("minutes"), 1, 43200, 1440))


def flow_items(svc, ident, **a):
    from .api.routers import bizflow as R
    return _route(R.items, "page.apm", ident, svc, fid=int(a["flow_id"]), state=a.get("state") or "stuck", minutes=_clamp(a.get("minutes"), 1, 43200, 1440),
                  limit=_clamp(a.get("limit"), 1, 200, 50))


def inventory_lookup(svc, ident, **a):
    from .api.routers import inventory as R
    if a.get("hostname"):
        return _route(R.get, "page.inv", ident, svc, hostname=str(a["hostname"]))
    return _route(R.list_hosts, "page.inv", ident, svc, q=str(a.get("q") or ""))[:_clamp(a.get("limit"), 1, 500, 100)]


def list_actions(svc, ident, **a):
    from .api.routers import actions as R
    rows = _route(R.list_actions, "page.actions", ident, svc, incident_id=a.get("incident_id") or None)
    if a.get("status"):
        rows = [r for r in rows if r.get("status") == a["status"]]
    return rows[:_clamp(a.get("limit"), 1, 500, 50)]


def get_action(svc, ident, **a):
    from .api.routers import actions as R
    return _route(R.get, "page.actions", ident, svc, aid=int(a["id"]))


def create_action(svc, ident, **a):
    from .api.routers import actions as R
    body = R.ActionIn(incident_id=str(a.get("incident_id") or "MCP"), title=str(a["title"])[:300], priority=str(a.get("priority") or "P2"),
                      owner=str(a.get("owner") or ""), recommendation=str(a.get("recommendation") or "")[:2000], host=str(a.get("host") or ""),
                      evidence=f"MCP key: {ident.name}")
    return _route(R.create, "page.actions", ident, svc, body=body)


def ack_anomaly(svc, ident, **a):
    from .api.routers import anomalies as R
    st = a.get("status") or "ack"
    if st not in ("ack", "resolved", "ignored", "open"):
        raise ToolError("bad_args", "status must be ack, resolved, ignored or open")
    body = R.StatusIn(status=st, owner=a.get("owner"), note=(f"[MCP {ident.name}] " + str(a.get("note") or "")).strip()[:500])
    return _route(R.set_status, "act.anomaly", ident, svc, aid=int(a["id"]), body=body)


def set_threshold(svc, ident, **a):
    """A threshold policy (the same as Eşikler › new policy): scope host / pool / env / all, warn and / or crit."""
    from .api.routers import thresholds as T
    scope, value = str(a.get("scope") or "host"), str(a.get("scope_value") or "")
    if scope == "host":
        if not value:
            raise ToolError("bad_args", "scope_value (the host) is required for scope=host")
        acc = ident.acc
        if not acc.install("sys.notify"):
            _host_ok(ident, "act.thresholds", value)
    body = T.PolicyIn(name=str(a.get("name") or f"MCP {a.get('metric')} {value}")[:120], scope=scope, scope_value=value, metric=str(a["metric"]),
                      warn=a.get("warn"), crit=a.get("crit"), for_min=_clamp(a.get("for_min"), 1, 1440, 3), channels="email", targets="",
                      note=(f"MCP key: {ident.name}. " + str(a.get("note") or ""))[:500], team_id=a.get("team_id"))
    return _route(T.create, "page.thr", ident, svc, body=body)


S, I, N, B = "string", "integer", "number", "boolean"
TOOLS: list = [
    Tool("whoami", "", False, "Anahtar kimliği ve kapsamı", "Key identity and scope", "Who this key acts for, its scope (teams / all hosts), write permission and the tools it may call.", {}, (), whoami),
    Tool("log_search", "page.logs", False, "Log arama (sorgu dili, zaman aralığı)", "Log search (query language, time range)",
         "Search log lines newest first with the Watchover query language (e.g. level:error service:payment \"timeout\" host:web-*). "
         "Returns lines, total and an opaque `next` cursor for the following page.",
         {"q": P(S, "Query in the Watchover log query language (empty = everything)"), **RANGE, "limit": P(I, "Lines per page (max 200)", maximum=200),
          "cursor": P(S, "The `next` value of the previous page")}, (), log_search),
    Tool("log_tail", "page.logs", False, "Canlı log kuyruğu (kısa anlık görüntü)", "Live log tail (short snapshot)",
         "The newest lines matching a query (max 100), or the lines after the line id `after` (from a previous tail).",
         {"q": P(S, "Query (log query language)"), "after": P(I, "Line id: only lines that arrived after it"), "limit": P(I, "Max lines (100)", maximum=100)}, (), log_tail),
    Tool("dash_infra", "page.ops", False, "Altyapı panosu (CPU / bellek / disk)", "Infrastructure dashboard (CPU / memory / disk)",
         "Fleet gauges with warn / crit levels, the top hosts and the per-server table for CPU, memory, disk and GPU over the range.",
         {**RANGE, **WHERE, "top": P(I, "How many hosts in the tables (max 50)")}, (), dash_infra),
    Tool("dash_alarms", "page.ops", False, "Alarm panosu", "Alarms dashboard",
         "Recent alarms from threshold breaches, anomalies and the alert log, in this key's scope.", {**RANGE, **WHERE, "limit": P(I, "Max alarms (200)")}, (), dash_alarms),
    Tool("dash_pools", "page.ops", False, "Pool panosu", "Pools dashboard", "Log lines and ERROR+ lines per host pool and time bar.",
         {**RANGE, **WHERE, "top": P(I, "Pools shown before folding into ~other (max 12)")}, (), dash_pools),
    Tool("host_detail", "page.ops", False, "Sunucu detayı (özet, alarm metrikleri, eşik seviyeleri)", "Host detail (summary, alarm metrics, threshold levels)",
         "One host: headline numbers over the range, current metric values with effective warn / crit levels and their source policy, open "
         "breaches, recent alarms and the inventory record.", {"host": P(S, "Host name"), **RANGE}, ("host",), host_detail),
    Tool("thresholds_state", "page.thr", False, "Eşik durumu", "Threshold state",
         "Open threshold breaches; with overview=true (or a host) also every host's current values and effective levels.",
         {"host": P(S, "Only this host"), "overview": P(B, "Include the per-host overview")}, (), thresholds_state),
    Tool("list_anomalies", "page.anom", False, "Anomaliler", "Anomalies", "Detected anomalies (status active / open / ack / resolved / all).",
         {"status": P(S, "active (default), open, ack, resolved, ignored or all"), "env": P(S, "Environment"), "kind": P(S, "Anomaly kind"),
          "limit": P(I, "Max rows (500)")}, (), list_anomalies),
    Tool("get_anomaly", "page.anom", False, "Anomali detayı", "Anomaly detail", "One anomaly with its checklist and history.", {"id": P(I, "Anomaly id")}, ("id",), get_anomaly),
    Tool("integration_hosts", "page.apm", False, "Entegrasyonu olan sunucular", "Hosts with integrations",
         "Hosts with network integrations (inbound / outbound counts, protocols).", {"minutes": P(I, "Window in minutes (default 1440)"), "env": P(S, "Environment")}, (), integration_hosts),
    Tool("host_integrations", "page.apm", False, "Sunucu entegrasyonları ve teknolojileri", "Host integrations with technology",
         "One host's integration map: callers, listening ports with processes, outbound integrations by category and technology (techstack), services seen.",
         {"host": P(S, "Host name"), "minutes": P(I, "Window in minutes (default 1440)")}, ("host",), host_integrations),
    Tool("search_traces", "page.apm", False, "İz (trace) arama", "Trace search", "Traces by service, operation, duration or errors.",
         {"service": P(S, "Service"), "operation": P(S, "Operation"), "minutes": P(I, "Window in minutes (default 60)"), "env": P(S, "Environment"),
          "min_ms": P(N, "Minimum duration (ms)"), "errors": P(B, "Only traces with errors"), "q": P(S, "Free text"), "session": P(S, "Session pseudonym"),
          "order": P(S, "recent or duration"), "limit": P(I, "Max traces (100)")}, (), search_traces),
    Tool("get_trace", "page.apm", False, "İz detayı", "Trace detail", "One trace as a span waterfall.", {"trace_id": P(S, "Trace id")}, ("trace_id",), get_trace),
    Tool("trace_session", "page.apm", False, "İzin istemci oturumu", "Client session of a trace",
         "The browser / app (or server-side) session behind a trace: device, the user step that started it and the steps around it. Ids are pseudonyms.",
         {"trace_id": P(S, "Trace id")}, ("trace_id",), trace_session),
    Tool("list_flows", "page.apm", False, "İş akışları", "Business flows", "Business flows with their throughput, stuck and failed counts.",
         {"minutes": P(I, "Window in minutes (default 1440)")}, (), list_flows),
    Tool("flow_funnel", "page.apm", False, "İş akışı hunisi", "Business flow funnel", "Per-step funnel of one business flow.",
         {"flow_id": P(I, "Flow id"), "minutes": P(I, "Window in minutes")}, ("flow_id",), flow_funnel),
    Tool("flow_items", "page.apm", False, "İş akışında takılan kalemler", "Business flow items",
         "Items of a flow in a state (stuck, failed, done).", {"flow_id": P(I, "Flow id"), "state": P(S, "stuck (default), failed or done"),
                                                                "minutes": P(I, "Window in minutes"), "limit": P(I, "Max items (200)")}, ("flow_id",), flow_items),
    Tool("inventory_lookup", "page.inv", False, "Envanter (CMDB) sorgusu", "Inventory (CMDB) lookup",
         "One inventory record by hostname, or a search over every field.", {"hostname": P(S, "Exact host name"), "q": P(S, "Search text"),
                                                                              "limit": P(I, "Max rows (500)")}, (), inventory_lookup),
    Tool("list_actions", "page.actions", False, "Aksiyonlar", "Actions", "Tracked actions (optionally of one incident or status).",
         {"incident_id": P(S, "Incident id"), "status": P(S, "Status filter"), "limit": P(I, "Max rows (500)")}, (), list_actions),
    Tool("get_action", "page.actions", False, "Aksiyon detayı", "Action detail", "One tracked action.", {"id": P(I, "Action id")}, ("id",), get_action),
    Tool("create_action", "page.actions", True, "Aksiyon oluştur (yazma)", "Create an action (write)",
         "Open a tracked action. Needs a key with the write permission.",
         {"title": P(S, "What to do"), "incident_id": P(S, "Incident id (default MCP)"), "priority": P(S, "P1..P4"), "owner": P(S, "Owner (team or person)"),
          "recommendation": P(S, "Details / recommendation"), "host": P(S, "Host the action is about")}, ("title",), create_action),
    Tool("ack_anomaly", "act.anomaly", True, "Anomaliyi onayla / kapat (yazma)", "Acknowledge / resolve an anomaly (write)",
         "Set an anomaly's status (ack by default) with a note. Needs a key with the write permission.",
         {"id": P(I, "Anomaly id"), "status": P(S, "ack (default), resolved, ignored or open"), "note": P(S, "Note"), "owner": P(S, "Owner")}, ("id",), ack_anomaly),
    Tool("set_threshold", "page.thr", True, "Eşik politikası tanımla (yazma)", "Set a threshold policy (write)",
         "Create a threshold policy (the team's when the key is a team's). Needs a key with the write permission and act.thresholds / sys.notify.",
         {"metric": P(S, "Metric (cpu, mem, disk, ...)"), "warn": P(N, "Warning level"), "crit": P(N, "Critical level"),
          "scope": P(S, "host (default), pool, env or all"), "scope_value": P(S, "The host / pool / env"), "for_min": P(I, "Minutes beyond the level before alerting"),
          "name": P(S, "Policy name"), "note": P(S, "Note"), "team_id": P(I, "Owning team (installation keys)")}, ("metric",), set_threshold),
]
BY_NAME = {t.name: t for t in TOOLS}


def visible(ident) -> list:
    """The tools this key may call: its permissions, and write tools only with the write permission."""
    acc = ident.acc
    return [t for t in TOOLS if (not t.perm or acc.can(t.perm)) and (not t.write or ident.write)]


# ---------------------------------------------------------------- output: JSON-safe, KVKK-masked, size-capped
def _scrub(o):
    from .privacy import scrub
    if isinstance(o, str):
        return scrub(o)
    if isinstance(o, list):
        return [_scrub(x) for x in o]
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()}
    return o


def _dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def _fit(o, cap: int = MAX_OUT):
    """Halve the longest top-level list until the JSON fits `cap` characters (marked truncated); else cut the text."""
    text = _dumps(o)
    if len(text) <= cap:
        return o, text
    for _ in range(40):
        if isinstance(o, list) and len(o) > 1:
            o = o[: len(o) // 2]
        elif isinstance(o, dict) and any(isinstance(v, list) and len(v) > 1 for v in o.values()):
            k = max((k for k, v in o.items() if isinstance(v, list) and len(v) > 1), key=lambda k: len(_dumps(o[k])))
            o = {**o, k: o[k][: len(o[k]) // 2]}
        else:
            break
        if len(_dumps(o)) <= cap - 40:
            o = {**o, "truncated": True} if isinstance(o, dict) else {"items": o, "truncated": True}
            return o, _dumps(o)
    o = {"truncated": True, "text": text[:cap]}
    return o, _dumps(o)


def _items(o) -> int:
    if isinstance(o, list):
        return len(o)
    if isinstance(o, dict):
        ls = [len(v) for v in o.values() if isinstance(v, list)]
        return max(ls) if ls else 1
    return 1


def call(svc, ident, name: str, args: Optional[dict] = None) -> dict:
    """tools/call: an MCP CallToolResult. Unknown tool -> KeyError (a protocol error); a refused or failed call -> isError."""
    from .mcpkeys import store
    t = BY_NAME.get(name or "")
    if t is None:
        raise KeyError(name)
    args = args if isinstance(args, dict) else {}
    t0 = time.perf_counter()
    try:
        if t.write and not ident.write:
            raise ToolError("read_only", "this key is read-only: create a key with the write (yazma) permission")
        if t.perm and not ident.acc.can(t.perm):
            raise ToolError("forbidden", f"this key lacks the permission {t.perm}")
        unknown = [k for k in args if k not in t.props]
        if unknown:
            raise ToolError("bad_args", f"unknown argument(s): {', '.join(sorted(unknown))[:200]}")
        missing = [k for k in t.required if args.get(k) in (None, "")]
        if missing:
            raise ToolError("bad_args", f"missing argument(s): {', '.join(missing)}")
        raw = t.fn(svc, ident, **args)
        out = _scrub(json.loads(json.dumps(raw, ensure_ascii=False, default=str)))
        out, text = _fit(out)
        store(svc).audit(ident, name, args.keys(), True, "", int((time.perf_counter() - t0) * 1000), _items(out))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        store(svc).audit(ident, name, args.keys(), False, e.code, int((time.perf_counter() - t0) * 1000), 0)
        return {"content": [{"type": "text", "text": json.dumps({"error": e.code, "message": e.message}, ensure_ascii=False)}], "isError": True}
    except Exception as e:  # noqa: BLE001 - one broken tool never takes the endpoint down
        store(svc).audit(ident, name, args.keys(), False, type(e).__name__, int((time.perf_counter() - t0) * 1000), 0)
        return {"content": [{"type": "text", "text": json.dumps({"error": "internal", "message": type(e).__name__})}], "isError": True}


# ---------------------------------------------------------------- JSON-RPC
def _err(mid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle(svc, ident, msg) -> Optional[dict]:
    """One JSON-RPC message -> its response (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None                                         # notifications/initialized, cancelled, ...
    if not isinstance(params, dict):
        return _err(mid, -32602, "params must be an object")
    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
               "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},
               "serverInfo": {"name": "watchover", "title": "Watchover", "version": __version__}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": [t.spec() for t in visible(ident)]}
    elif method == "tools/call":
        try:
            res = call(svc, ident, str(params.get("name") or ""), params.get("arguments"))
        except KeyError:
            return _err(mid, -32602, f"unknown tool: {str(params.get('name'))[:80]}")
    elif method == "resources/list":
        res = {"resources": []}
    elif method == "resources/templates/list":
        res = {"resourceTemplates": []}
    elif method == "prompts/list":
        res = {"prompts": []}
    else:
        return _err(mid, -32601, f"method not found: {method[:80]}")
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def handle_body(svc, ident, body: Any):
    """A POSTed body (one message or a batch) -> the response body, or None when only notifications / responses were sent."""
    if isinstance(body, list):
        out = [r for r in (handle(svc, ident, m) for m in body[:50]) if r is not None]
        return out or None
    return handle(svc, ident, body)
