"""Error map: a service / host dependency graph built from the observations themselves.

Nodes are services (boxes) and hosts (ellipses, clustered by environment). Edges:
  * dependency  -- "upstream X", "timeout to db-01", "calling Y" in error messages (service -> dependency)
  * correlation -- the engine's incident links (shared entity within the window), service <-> service
  * host        -- host -> service: ERROR+ events that service produced on that host
The root-cause service of the chosen incident is highlighted; the impact chain follows
dependency edges backwards from the root cause (who depends on the failing thing).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .models import SEV_RANK, Observation

DEP_RE = re.compile(
    r"(?:upstream|downstream|to|from|calling|call to|connect(?:ing|ion)? to|dependency)\s+"
    r"((?:[a-z][a-z0-9_.-]*?)?(?:-api|-service|-svc|-db|-cache|-queue|-gateway|-proxy|-worker|db-\d+|db|postgres(?:ql)?|mysql|redis|kafka|rabbitmq|elasticsearch|mongo(?:db)?|cache))\b",
    re.I)

PALETTE = {"root": "#ff5c5c", "affected": "#ffb347", "erroring": "#f2d55c", "clean": "#3ddc84", "host": "#1a1f26"}


def dependency_edges(observations: list[Observation], errors_only: bool = True) -> Counter:
    """(service -> dependency) counts from message text. Skips self references."""
    out: Counter = Counter()
    for o in observations:
        if errors_only and SEV_RANK[o.severity] < 3:
            continue
        m = DEP_RE.search(o.message)
        if not m or not o.service:
            continue
        target = m[1].lower()
        if target != o.service.lower():
            out[(o.service, target)] += 1
    return out


def build_map(analysis, incident_id: str | None = None, env: str | None = None, host: str | None = None,
              with_hosts: bool = True) -> dict:
    """Nodes / edges for the error map. incident_id=None means every incident (root causes of all)."""
    obs = [o for o in analysis.observations if (not env or (o.environment or "unknown") == env) and (not host or o.host == host)]
    incs = [i for i in analysis.incidents if incident_id is None or i.id == incident_id]
    root_svcs: set[str] = set()
    affected: set[str] = set()
    links: list[dict] = []
    for inc in incs:
        root = analysis.signal_by_id.get(inc.root_cause_signal)
        if root:
            root_svcs |= set(root.services)
        affected |= set(inc.affected_services)
        links += inc.links
    errs: Counter = Counter()
    host_svc: Counter = Counter()
    host_env: dict[str, str] = {}
    for o in obs:
        if o.host:
            host_env.setdefault(o.host, o.environment or "unknown")
        if SEV_RANK[o.severity] >= 3:
            errs[o.service or "-"] += 1
            if o.host and o.service:
                host_svc[(o.host, o.service)] += 1
    # a dependency named after a host ("timeout to db-01") is mapped to the service(s) that run there
    svc_on_host: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        if o.host and o.service:
            svc_on_host[o.host.lower()].add(o.service)
    raw = dependency_edges(obs)
    deps: Counter = Counter()
    for (s, d), n in raw.items():
        targets = svc_on_host.get(d) or {d}
        if len(targets) > 1:               # several services on that host: keep the ones the incident points at
            hit = targets & (affected | root_svcs)
            targets = hit or {x for x in targets if errs.get(x)} or targets
        for tgt in targets:
            if tgt != s:
                deps[(s, tgt)] += n
    services = set(affected) | root_svcs | {s for s, _ in deps} | {d for _, d in deps}
    if not services:                       # nothing correlated: fall back to erroring services
        services = {s for s, n in errs.items() if n and s != "-"}
    # correlation edges between services of linked signals (skip pairs already explained by a dependency)
    corr: dict[tuple[str, str], dict] = {}
    for e in links:
        sa, sb = analysis.signal_by_id.get(e["a"]), analysis.signal_by_id.get(e["b"])
        if not sa or not sb:
            continue
        for x in sa.services:
            for y in sb.services:
                if x == y or (x, y) in deps or (y, x) in deps:
                    continue
                key = tuple(sorted((x, y)))
                cur = corr.setdefault(key, {"ents": set(), "gap": int(float(e.get("gap", 0)))})
                cur["ents"] |= set(e.get("ents", []))
                cur["gap"] = min(cur["gap"], int(float(e.get("gap", 0))))
                services |= {x, y}
    nodes = []
    for s in sorted(services):
        kind = "root" if s in root_svcs else "affected" if s in affected else "erroring" if errs.get(s) else "clean"
        nodes.append({"id": s, "kind": kind, "errors": errs.get(s, 0), "type": "service"})
    hosts = []
    if with_hosts:
        for (h, s), n in host_svc.items():
            if s in services:
                hosts.append({"host": h, "service": s, "errors": n, "env": host_env.get(h, "unknown")})
    # impact chain: from each root cause, walk dependency edges backwards (dependents)
    rev: dict[str, list[str]] = defaultdict(list)
    for (s, d), _ in deps.items():
        rev[d].append(s)
    chain: list[str] = []
    seen: set[str] = set()
    frontier = sorted(root_svcs)
    while frontier:
        nxt = []
        for r in frontier:
            if r in seen:
                continue
            seen.add(r)
            chain.append(r)
            nxt += sorted(x for x in rev.get(r, []) if x not in seen)
        frontier = nxt
    return {"nodes": nodes, "deps": [{"from": s, "to": d, "n": n} for (s, d), n in deps.most_common()],
            "corr": [{"a": a, "b": b, "ents": sorted(v["ents"]), "gap": v["gap"]} for (a, b), v in corr.items()],
            "hosts": hosts, "root": sorted(root_svcs), "affected": sorted(affected), "chain": chain,
            "incidents": [i.id for i in incs], "errors_total": sum(errs.values())}


def to_dot(m: dict, labels: dict | None = None) -> str:
    """Graphviz DOT for st.graphviz_chart (dark theme)."""
    L = {"root": "ROOT CAUSE", "dep": "dependency errors", "hosts": "hosts", "errors": "ERROR+", **(labels or {})}
    esc = lambda s: str(s).replace('"', '\\"')
    out = ['digraph G {', 'rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.9; pad=0.2;',
           'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, color="#262b33", penwidth=1, margin="0.18,0.1"];',
           'edge [fontname="Helvetica", fontsize=10, fontcolor="#c9d1d9", color="#6b7a90", arrowsize=0.8];']
    for n in m["nodes"]:
        fill = PALETTE[n["kind"]]
        font = "#0b0d10"
        lbl = f'{esc(n["id"])}\\n{n["errors"]} {L["errors"]}' + (f'\\n⚑ {L["root"]}' if n["kind"] == "root" else "")
        pw = 3 if n["kind"] == "root" else 1.2
        out.append(f'"{esc(n["id"])}" [label="{lbl}", fillcolor="{fill}", fontcolor="{font}", penwidth={pw}, color="{"#ffffff" if n["kind"] == "root" else "#262b33"}"];')
    for e in m["deps"]:
        out.append(f'"{esc(e["from"])}" -> "{esc(e["to"])}" [label="{e["n"]}× {L["dep"]}", color="#ff5c5c", fontcolor="#ff9b9b", penwidth={1 + min(e["n"], 40) / 10:.1f}];')
    for e in m["corr"]:
        lbl = ", ".join(e["ents"][:2]) + (f' · {e["gap"]}s' if e["gap"] else "")
        out.append(f'"{esc(e["a"])}" -> "{esc(e["b"])}" [style=dashed, arrowhead=none, label="{esc(lbl)}", color="#6b9bd2", fontcolor="#9fb3c8"];')
    by_env: dict[str, list[dict]] = defaultdict(list)
    for h in m["hosts"]:
        by_env[h["env"]].append(h)
    for i, (env, hs) in enumerate(sorted(by_env.items())):
        out.append(f'subgraph cluster_env{i} {{ label="{esc(env)} · {L["hosts"]}"; fontcolor="#8b93a1"; fontname="Helvetica"; fontsize=11; color="#262b33"; style="rounded"; bgcolor="#0f1216";')
        out.append('node [shape=ellipse, fillcolor="#1a1f26", fontcolor="#e6edf3", color="#3a4150", style="filled"];')
        for h in sorted({h["host"] for h in hs}):
            out.append(f'"{esc(h)}";')
        out.append("}")
    for h in m["hosts"]:
        out.append(f'"{esc(h["host"])}" -> "{esc(h["service"])}" [label="{h["errors"]}", color="#4a5361", fontcolor="#8b93a1", arrowhead=vee, penwidth={0.8 + min(h["errors"], 30) / 20:.1f}];')
    out.append("}")
    return "\n".join(out)
