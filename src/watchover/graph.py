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

from . import scenario
from .models import SEV_RANK, Observation

DEP_RE = re.compile(
    r"(?:upstream|downstream|to|from|calling|call to|connect(?:ing|ion)? to|dependency)\s+"
    r"((?:[a-z][a-z0-9_.-]*?)?(?:-api|-service|-svc|-db|-cache|-queue|-gateway|-proxy|-worker|db-\d+|db|postgres(?:ql)?|mysql|redis|kafka|rabbitmq|elasticsearch|mongo(?:db)?|cache))\b",
    re.I)

PALETTE = {"root": "#f87171", "affected": "#fb923c", "erroring": "#fbbf24", "clean": "#2dd4bf", "host": "#1a2333"}


def dependency_edges(observations: list[Observation], errors_only: bool = True) -> Counter:
    """(service -> dependency) counts from message text. Skips self references."""
    out: Counter = Counter()
    extra = [re.compile(p, re.I) for p in getattr(scenario, "DEP_PATTERNS", [])]
    for o in observations:
        if errors_only and SEV_RANK[o.severity] < 3:
            continue
        m = DEP_RE.search(o.message)
        if not m:
            for pat in extra:
                m = pat.search(o.message)
                if m:
                    break
        if not m or not o.service:
            continue
        target = m[1].lower()
        if target != o.service.lower():
            out[(o.service, target)] += 1
    return out


def build_map(analysis, incident_id: str | None = None, env: str | None = None, host: str | None = None,
              with_hosts: bool = True) -> dict:
    """Nodes / edges for the error map. incident_id=None means every incident (root causes of all)."""
    obs = [o for o in analysis.observations if (not env or (o.environment or "unknown") == env) and (not host or (o.host == host if isinstance(host, str) else o.host in host))]
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
    if env or host:                        # a narrowed scope only keeps services that actually appear in it
        present = {o.service for o in obs if o.service}
        root_svcs &= present
        affected &= present
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
    declared = [{"from": d["source"], "to": d["target"], "n": 0, "type": d.get("type", ""), "declared": True}
                for d in getattr(analysis, "dependencies", []) or []
                if d["source"] in services and d["target"] in services and (d["source"], d["target"]) not in deps]
    return {"nodes": nodes, "deps": [{"from": s, "to": d, "n": n} for (s, d), n in deps.most_common()] + declared,
            "corr": [{"a": a, "b": b, "ents": sorted(v["ents"]), "gap": v["gap"]} for (a, b), v in corr.items()],
            "hosts": hosts, "root": sorted(root_svcs), "affected": sorted(affected), "chain": chain,
            "incidents": [i.id for i in incs], "errors_total": sum(errs.values())}


def to_dot(m: dict, labels: dict | None = None) -> str:
    """Graphviz DOT for st.graphviz_chart (dark theme)."""
    L = {"root": "ROOT CAUSE", "dep": "dependency errors", "hosts": "hosts", "errors": "ERROR+", **(labels or {})}
    esc = lambda s: str(s).replace('"', '\\"')
    out = ['digraph G {', 'rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.9; pad=0.2;',
           'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, color="#243044", penwidth=1, margin="0.18,0.1"];',
           'edge [fontname="Helvetica", fontsize=10, fontcolor="#cbd5e1", color="#64748b", arrowsize=0.8];']
    for n in m["nodes"]:
        fill = PALETTE[n["kind"]]
        font = "#0e1420"
        lbl = f'{esc(n["id"])}\\n{n["errors"]} {L["errors"]}' + (f'\\n⚑ {L["root"]}' if n["kind"] == "root" else "")
        pw = 3 if n["kind"] == "root" else 1.2
        out.append(f'"{esc(n["id"])}" [label="{lbl}", fillcolor="{fill}", fontcolor="{font}", penwidth={pw}, color="{"#ffffff" if n["kind"] == "root" else "#243044"}"];')
    for e in m["deps"]:
        out.append(f'"{esc(e["from"])}" -> "{esc(e["to"])}" [label="{e["n"]}× {L["dep"]}", color="#f87171", fontcolor="#fca5a5", penwidth={1 + min(e["n"], 40) / 10:.1f}];')
    for e in m["corr"]:
        lbl = ", ".join(e["ents"][:2]) + (f' · {e["gap"]}s' if e["gap"] else "")
        out.append(f'"{esc(e["a"])}" -> "{esc(e["b"])}" [style=dashed, arrowhead=none, label="{esc(lbl)}", color="#60a5fa", fontcolor="#a5b4c9"];')
    by_env: dict[str, list[dict]] = defaultdict(list)
    for h in m["hosts"]:
        by_env[h["env"]].append(h)
    for i, (env, hs) in enumerate(sorted(by_env.items())):
        out.append(f'subgraph cluster_env{i} {{ label="{esc(env)} · {L["hosts"]}"; fontcolor="#8b98ad"; fontname="Helvetica"; fontsize=11; color="#243044"; style="rounded"; bgcolor="#111827";')
        out.append('node [shape=ellipse, fillcolor="#1a2333", fontcolor="#e6ebf2", color="#334155", style="filled"];')
        for h in sorted({h["host"] for h in hs}):
            out.append(f'"{esc(h)}";')
        out.append("}")
    for h in m["hosts"]:
        out.append(f'"{esc(h["host"])}" -> "{esc(h["service"])}" [label="{h["errors"]}", color="#475569", fontcolor="#8b98ad", arrowhead=vee, penwidth={0.8 + min(h["errors"], 30) / 20:.1f}];')
    out.append("}")
    return "\n".join(out)


# ---------------------------------------------------------------- interactive HTML (no external libraries)
def layout(m: dict, col_w: int = 250, row_h: int = 84) -> dict:
    """Left-to-right layered coordinates: hosts (grouped by environment) -> dependents -> ... -> root cause."""
    deps = m["deps"]
    depth: dict[str, int] = {r: 0 for r in m["root"]}
    rev: dict[str, list[str]] = defaultdict(list)
    fwd: dict[str, list[str]] = defaultdict(list)
    for e in deps:
        rev[e["to"]].append(e["from"])
        fwd[e["from"]].append(e["to"])
    frontier = list(depth)
    while frontier:                         # dependents sit one column to the left of what they depend on
        nxt = []
        for r in frontier:
            for x in rev.get(r, []):
                if x not in depth:
                    depth[x] = depth[r] + 1
                    nxt.append(x)
        frontier = nxt
    ids = [n["id"] for n in m["nodes"]]
    for sid in ids:                         # unplaced: next to its dependency, else the middle
        if sid not in depth:
            tgt = [depth[t] for t in fwd.get(sid, []) if t in depth]
            depth[sid] = (min(tgt) + 1) if tgt else 1
    maxd = max(depth.values()) if depth else 0
    cols: dict[int, list[str]] = defaultdict(list)
    for sid in ids:
        cols[maxd - depth[sid]].append(sid)
    pos: dict[str, tuple[float, float]] = {}
    hosts = sorted({(h["env"], h["host"]) for h in m["hosts"]})
    n_rows = max([len(v) for v in cols.values()] + [len(hosts) + len({e for e, _ in hosts})])
    height = max(3, n_rows) * row_h + 60
    x0 = 150 if hosts else 40
    for c, members in cols.items():
        x = x0 + (c + 1) * col_w
        top = (height - len(members) * row_h) / 2
        for i, sid in enumerate(sorted(members)):
            pos[sid] = (x, top + i * row_h + row_h / 2)
    hpos: dict[str, tuple[float, float]] = {}
    groups: list[dict] = []
    if hosts:
        rows = len(hosts) + len({e for e, _ in hosts})
        y = (height - rows * row_h * 0.8) / 2 + 20
        cur = None
        for envn, h in hosts:
            if envn != cur:
                cur = envn
                groups.append({"env": envn, "y0": y - 6, "hosts": []})
                y += row_h * 0.55
            hpos[h] = (x0, y)
            groups[-1]["hosts"].append(h)
            groups[-1]["y1"] = y + row_h * 0.45
            y += row_h * 0.8
    width = x0 + (maxd + 2) * col_w + 60
    return {"pos": pos, "hpos": hpos, "groups": groups, "width": int(width), "height": int(height)}


def to_html(m: dict, labels: dict | None = None, signals: dict | None = None, height: int = 620) -> str:
    """Self-contained interactive map: pan / zoom, click a node to focus it and open its detail panel."""
    import json
    L = {"root": "ROOT CAUSE", "dep": "dependency errors", "hosts": "hosts", "errors": "ERROR+", "signals": "signals",
         "depends_on": "Depends on", "depended_by": "Depended on by", "on_hosts": "Hosts", "reset": "Reset view",
         "hint": "click a node · drag to pan · wheel to zoom", "kind_root": "root cause", "kind_affected": "affected",
         "kind_erroring": "erroring", "kind_clean": "clean", "host": "host", **(labels or {})}
    lay = layout(m)
    data = {"nodes": m["nodes"], "deps": m["deps"], "corr": m["corr"], "hosts": m["hosts"], "chain": m["chain"],
            "pos": lay["pos"], "hpos": lay["hpos"], "groups": lay["groups"], "w": lay["width"], "h": lay["height"],
            "signals": signals or {}, "L": L, "pal": PALETTE}
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", payload).replace("__H__", str(height))


HTML_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent;font-family:Helvetica,Arial,sans-serif;color:#e6ebf2;overflow:hidden}
#wrap{position:relative;width:100%;height:__H__px;border:1px solid #243044;border-radius:14px;background:radial-gradient(1200px 600px at 30% 20%,#12161c 0%,#0e1420 70%);overflow:hidden}
svg{width:100%;height:100%;cursor:grab}svg.drag{cursor:grabbing}
.node{cursor:pointer}.node rect{filter:drop-shadow(0 6px 14px rgba(0,0,0,.55))}
.node text{pointer-events:none;font-size:12px}.node .t{font-weight:700;fill:#0e1420}.node .s{fill:#0e1420;opacity:.75;font-size:11px}
.host ellipse{fill:#1a2333;stroke:#334155;stroke-width:1.2}.host text{fill:#e6ebf2;font-size:11px;pointer-events:none}
.edge{fill:none}.elabel{font-size:10px;fill:#cbd5e1;pointer-events:none}
.dim{opacity:.13;transition:opacity .25s}.lit{opacity:1;transition:opacity .25s}
.pulse{animation:pulse 1.6s ease-in-out infinite}@keyframes pulse{0%,100%{stroke-opacity:1}50%{stroke-opacity:.25}}
.glabel{font-size:11px;fill:#8b98ad;text-transform:uppercase;letter-spacing:.7px}
#panel{position:absolute;top:14px;right:14px;width:300px;max-height:calc(100% - 28px);overflow:auto;background:rgba(17,20,26,.96);border:1px solid #1f2a3d;border-radius:12px;padding:14px 16px;display:none;box-shadow:0 10px 30px rgba(0,0,0,.5)}
#panel h3{margin:0 0 4px;font-size:16px}#panel .k{color:#8b98ad;font-size:11px;text-transform:uppercase;letter-spacing:.7px;margin-top:10px}
#panel .sig{background:#111827;border:1px solid #243044;border-radius:8px;padding:6px 8px;margin:6px 0;font-size:12px}
#panel .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#cbd5e1;word-break:break-all}
#panel .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;color:#0e1420;margin-right:4px}
#panel .x{position:absolute;top:8px;right:10px;cursor:pointer;color:#8b98ad;font-size:16px}
#hint{position:absolute;left:14px;bottom:10px;font-size:11px;color:#64748b}
#reset{position:absolute;left:14px;top:12px;font-size:11px;color:#cbd5e1;background:#161b22;border:1px solid #1f2a3d;border-radius:8px;padding:4px 10px;cursor:pointer;display:none}
</style></head><body><div id="wrap">
<svg id="svg" xmlns="http://www.w3.org/2000/svg"><defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#f87171"/></marker>
<marker id="ag" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#64748b"/></marker>
</defs><g id="root"></g></svg>
<div id="reset">⟲ <span id="resetl"></span></div><div id="hint"></div><div id="panel"><span class="x" id="close">✕</span><div id="pbody"></div></div>
</div><script>
const D=__DATA__;const L=D.L;const NS="http://www.w3.org/2000/svg";
const svg=document.getElementById("svg"),root=document.getElementById("root"),panel=document.getElementById("panel"),pbody=document.getElementById("pbody");
document.getElementById("hint").textContent=L.hint;document.getElementById("resetl").textContent=L.reset;
const W=svg.clientWidth||1200,H=svg.clientHeight||__H__;const NW=150,NH=52;
let k=Math.min(W/(D.w+40),H/(D.h+40),1.15),tx=(W-D.w*k)/2,ty=(H-D.h*k)/2,sel=null;
function el(n,a,t){const e=document.createElementNS(NS,n);for(const x in a)e.setAttribute(x,a[x]);if(t!=null)e.textContent=t;return e}
function apply(){root.setAttribute("transform",`translate(${tx},${ty}) scale(${k})`)}
const nodeOf={};D.nodes.forEach(n=>nodeOf[n.id]=n);
const adj={};function link(a,b){(adj[a]=adj[a]||new Set()).add(b);(adj[b]=adj[b]||new Set()).add(a)}
D.deps.forEach(e=>link(e.from,e.to));D.corr.forEach(e=>link(e.a,e.b));D.hosts.forEach(h=>link(h.host,h.service));
const edgeEls=[],nodeEls={};
function curve(x1,y1,x2,y2){const dx=Math.max(60,Math.abs(x2-x1)*.45);return `M${x1} ${y1} C${x1+dx} ${y1} ${x2-dx} ${y2} ${x2} ${y2}`}
// environment groups
D.groups.forEach(g=>{const x=D.hpos[g.hosts[0]][0];root.appendChild(el("rect",{x:x-80,y:g.y0-18,width:160,height:g.y1-g.y0+24,rx:12,fill:"#111827",stroke:"#243044"}));root.appendChild(el("text",{x:x-70,y:g.y0-4,class:"glabel"},`${g.env} · ${L.hosts}`))});
// edges
function addEdge(a,b,x1,y1,x2,y2,attrs,label,lcolor){const p=el("path",Object.assign({d:curve(x1,y1,x2,y2),class:"edge"},attrs));root.appendChild(p);const t=el("text",{x:(x1+x2)/2,y:(y1+y2)/2-6,class:"elabel","text-anchor":"middle",fill:lcolor||"#cbd5e1"},label);root.appendChild(t);edgeEls.push({a,b,els:[p,t]})}
D.hosts.forEach(h=>{const [x1,y1]=D.hpos[h.host],[x2,y2]=D.pos[h.service]||[0,0];addEdge(h.host,h.service,x1+60,y1,x2-NW/2,y2,{stroke:"#475569","stroke-width":.8+Math.min(h.errors,30)/20,"marker-end":"url(#ag)"},String(h.errors),"#8b98ad")});
D.corr.forEach(e=>{const [x1,y1]=D.pos[e.a],[x2,y2]=D.pos[e.b];addEdge(e.a,e.b,x1,y1-NH/2,x2,y2-NH/2,{stroke:"#60a5fa","stroke-width":1.2,"stroke-dasharray":"5 4"},(e.ents.slice(0,2).join(", ")+(e.gap?` · ${e.gap}s`:"")),"#a5b4c9")});
D.deps.forEach(e=>{const [x1,y1]=D.pos[e.from],[x2,y2]=D.pos[e.to];const left=x1<x2;if(e.declared){addEdge(e.from,e.to,left?x1+NW/2:x1-NW/2,y1,left?x2-NW/2:x2+NW/2,y2,{stroke:"#a78bfa","stroke-width":1,"stroke-dasharray":"2 3","marker-end":"url(#ag)"},e.type||"",'#a78bfa')}else{addEdge(e.from,e.to,left?x1+NW/2:x1-NW/2,y1,left?x2-NW/2:x2+NW/2,y2,{stroke:"#f87171","stroke-width":1+Math.min(e.n,40)/10,"marker-end":"url(#ar)",class:"edge pulse"},`${e.n}× ${L.dep}`,"#fca5a5")}});
// hosts
for(const h in D.hpos){const [x,y]=D.hpos[h];const g=el("g",{class:"host node"});g.appendChild(el("ellipse",{cx:x,cy:y,rx:60,ry:17}));g.appendChild(el("text",{x,y:y+4,"text-anchor":"middle"},h));g.addEventListener("click",ev=>{ev.stopPropagation();focus(h)});root.appendChild(g);nodeEls[h]=g}
// services
D.nodes.forEach(n=>{const [x,y]=D.pos[n.id];const g=el("g",{class:"node"});const rootc=n.kind==="root";
g.appendChild(el("rect",{x:x-NW/2,y:y-NH/2,width:NW,height:NH,rx:12,fill:D.pal[n.kind],stroke:rootc?"#fff":"#243044","stroke-width":rootc?3:1.2}));
g.appendChild(el("text",{x,y:y-4,"text-anchor":"middle",class:"t"},n.id));g.appendChild(el("text",{x,y:y+13,"text-anchor":"middle",class:"s"},`${n.errors} ${L.errors}`+(rootc?`  ⚑ ${L.root}`:"")));
g.addEventListener("click",ev=>{ev.stopPropagation();focus(n.id)});root.appendChild(g);nodeEls[n.id]=g});
apply();
// interaction
let drag=null;svg.addEventListener("mousedown",e=>{drag={x:e.clientX,y:e.clientY,tx,ty};svg.classList.add("drag")});
window.addEventListener("mousemove",e=>{if(!drag)return;tx=drag.tx+e.clientX-drag.x;ty=drag.ty+e.clientY-drag.y;apply()});
window.addEventListener("mouseup",()=>{drag=null;svg.classList.remove("drag");save()});
svg.addEventListener("wheel",e=>{e.preventDefault();const r=svg.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,f=e.deltaY<0?1.12:1/1.12;tx=mx-(mx-tx)*f;ty=my-(my-ty)*f;k*=f;apply();save()},{passive:false});
svg.addEventListener("click",()=>reset());document.getElementById("close").onclick=()=>reset();document.getElementById("reset").onclick=()=>reset();
function animate(tk,ttx,tty){const k0=k,x0=tx,y0=ty,t0=performance.now();(function step(t){const p=Math.min(1,(t-t0)/320),e=1-Math.pow(1-p,3);k=k0+(tk-k0)*e;tx=x0+(ttx-x0)*e;ty=y0+(tty-y0)*e;apply();if(p<1)requestAnimationFrame(step);else save()})(t0)}
function fitK(){return Math.min(W/(D.w+40),H/(D.h+40),1.15)}
function focus(id,instant){sel=id;const [x,y]=D.pos[id]||D.hpos[id];const tk=Math.max(0.9,Math.min(1.35,fitK()*1.35)),ttx=(W-330)/2-x*tk,tty=H/2-y*tk;if(instant){k=tk;tx=ttx;ty=tty;apply()}else animate(tk,ttx,tty);
const near=adj[id]||new Set();for(const n in nodeEls)nodeEls[n].setAttribute("class",(n===id||near.has(n))?"node lit"+(nodeEls[n].classList.contains("host")?" host":""):"node dim"+(nodeEls[n].classList.contains("host")?" host":""));
edgeEls.forEach(e=>e.els.forEach(x=>x.classList.toggle("dim",!(e.a===id||e.b===id))));document.getElementById("reset").style.display="block";show(id);save()}
function reset(){sel=null;for(const n in nodeEls)nodeEls[n].classList.remove("dim");edgeEls.forEach(e=>e.els.forEach(x=>x.classList.remove("dim")));panel.style.display="none";document.getElementById("reset").style.display="none";animate(fitK(),(W-D.w*fitK())/2,(H-D.h*fitK())/2)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function show(id){const n=nodeOf[id];let h="";if(n){h+=`<h3>${esc(id)}</h3><span class="pill" style="background:${D.pal[n.kind]}">${esc(L["kind_"+n.kind])}</span> <span style="color:#8b98ad;font-size:12px">${n.errors} ${L.errors}</span>`;
const out=D.deps.filter(e=>e.from===id),inn=D.deps.filter(e=>e.to===id),hs=D.hosts.filter(x=>x.service===id);
if(out.length)h+=`<div class="k">${L.depends_on}</div>`+out.map(e=>`<div>→ <b>${esc(e.to)}</b> <span style="color:${e.declared?'#a78bfa':'#fca5a5'}">${e.declared?(e.type||'·'):e.n+'×'}</span></div>`).join("");
if(inn.length)h+=`<div class="k">${L.depended_by}</div>`+inn.map(e=>`<div>← <b>${esc(e.from)}</b> <span style="color:${e.declared?'#a78bfa':'#fca5a5'}">${e.declared?(e.type||'·'):e.n+'×'}</span></div>`).join("");
if(hs.length)h+=`<div class="k">${L.on_hosts}</div><div>`+hs.map(x=>`${esc(x.host)} <span style="color:#8b98ad">(${x.errors})</span>`).join(" · ")+"</div>";
const sg=D.signals[id]||[];if(sg.length)h+=`<div class="k">${sg.length} ${L.signals}</div>`+sg.map(s=>`<div class="sig"><b style="color:${s.color}">${esc(s.severity)}</b> <span style="color:#8b98ad">×${s.count}${s.hosts?" · "+esc(s.hosts):""}</span><div class="mono">${esc(s.template)}</div></div>`).join("")}
else{const hs=D.hosts.filter(x=>x.host===id);h+=`<h3>${esc(id)}</h3><span class="pill" style="background:#60a5fa">${L.host}</span>`+(hs.length?`<div class="k">${L.errors}</div>`+hs.map(x=>`<div><b>${esc(x.service)}</b> <span style="color:#fca5a5">${x.errors}</span> <span style="color:#8b98ad">· ${esc(x.env)}</span></div>`).join(""):"")}
pbody.innerHTML=h;panel.style.display="block"}
function save(){try{window.name=JSON.stringify({sel,k,tx,ty})}catch(e){}}
try{const st=JSON.parse(window.name||"null");if(st&&st.sel&&(D.pos[st.sel]||D.hpos[st.sel])){focus(st.sel,true)}else if(st&&st.k){k=st.k;tx=st.tx;ty=st.ty;apply()}}catch(e){}
</script></body></html>"""
