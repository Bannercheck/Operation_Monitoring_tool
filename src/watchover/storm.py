"""Alert-storm clustering (S-A1): density cells + dependency / time adjacency.

Why not fingerprints here: in an alert storm one event spreads over dozens of alarm types and severities,
each with a handful of alarms, while background noise hits every service at a steady rate. Grouping by
message template chains everything together. Instead:

1. Cut the window into BUCKET_MIN buckets. For every service and every host count alarms per bucket.
2. A (service, bucket) cell is HOT when its count >= max(HOT_MIN, HOT_RATIO x that service's median bucket
   count). Same for hosts (HOST_HOT_MIN); a hot host cell lights up its service cell.
3. Hot cells link when they are at most one bucket apart AND the services are the same, one depends on
   the other (service_dependencies), or both are infrastructure alarms in the same rack.
4. Connected hot cells = one incident. Its alarms are all alarms of the member services / hosts inside the
   incident's time span. Everything else is noise, with the reason "inside the service's normal rate".

Every threshold lives in scenario/ so the jury can see (and we can defend) the knobs.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from . import scenario
from .models import Observation

INFRA_WORDS = ("arayuz", "link", "paket kaybi", "network", "interface", "packet loss", "switch", "power", "guc", "koptu")


def _bucket(o: Observation, t0, size_min: int) -> int:
    return int((o.timestamp - t0).total_seconds() // (size_min * 60))


def _is_infra(o: Observation) -> bool:
    typ = str(o.attributes.get("alarm_type", "")).lower()
    if typ:
        return typ in scenario.INFRA_TYPES
    m = o.message.lower()
    return any(w in m for w in INFRA_WORDS)


def _rack(o: Observation) -> str:
    return f"{o.attributes.get('tags.veri_merkezi') or o.attributes.get('veri_merkezi') or ''}/{o.attributes.get('tags.kabin') or o.attributes.get('kabin') or ''}"


def cluster(observations: list[Observation], deps: list[dict], inventory: dict | None = None) -> dict:
    """Returns {"clusters": [{"obs": [...], "cells": [(svc, b)], "hot": {...}, "why": [...]}], "noise": [obs], "cells": {...}, "t0": t0}"""
    if not observations:
        return {"clusters": [], "noise": [], "cells": {}, "t0": None, "bucket_min": scenario.BUCKET_MIN}
    size = scenario.BUCKET_MIN
    t0 = min(o.timestamp for o in observations).replace(second=0, microsecond=0)
    n_buckets = _bucket(max(observations, key=lambda o: o.timestamp), t0, size) + 1
    svc_cells: dict[str, Counter] = defaultdict(Counter)
    host_cells: dict[str, Counter] = defaultdict(Counter)
    host_svc: dict[str, str] = {}
    for o in observations:
        b = _bucket(o, t0, size)
        if o.service:
            svc_cells[o.service][b] += 1
        if o.host:
            host_cells[o.host][b] += 1
            host_svc.setdefault(o.host, o.service or (inventory or {}).get(o.host, {}).get("service", ""))
    hot: dict[tuple[str, int], dict] = {}          # (service, bucket) -> why it is hot
    for svc, c in svc_cells.items():
        vals = [c.get(i, 0) for i in range(n_buckets)]
        med = statistics.median(vals)
        thr = max(scenario.HOT_MIN, scenario.HOT_RATIO * med)
        for b, n in c.items():
            if n >= thr:
                hot[(svc, b)] = {"count": n, "median": med, "threshold": thr, "by": "service"}
    for h, c in host_cells.items():
        svc = host_svc.get(h, "")
        if not svc:
            continue
        vals = [c.get(i, 0) for i in range(n_buckets)]
        med = statistics.median(vals)
        thr = max(scenario.HOST_HOT_MIN, scenario.HOT_RATIO * med)
        for b, n in c.items():
            if n >= thr and (svc, b) not in hot:
                hot[(svc, b)] = {"count": n, "median": med, "threshold": thr, "by": f"host {h}"}
    # adjacency between hot cells
    pairs = {(d["source"].lower(), d["target"].lower()) for d in deps}
    rack_of_cell: dict[tuple[str, int], Counter] = defaultdict(Counter)
    infra_cell: Counter = Counter()
    for o in observations:
        key = (o.service, _bucket(o, t0, size))
        if key in hot and _is_infra(o):
            infra_cell[key] += 1
            rack_of_cell[key][_rack(o)] += 1

    def related(a: tuple[str, int], b: tuple[str, int]) -> str | None:
        if abs(a[1] - b[1]) > 1:
            return None
        if a[0] == b[0]:
            return "same_service"
        if (a[0].lower(), b[0].lower()) in pairs or (b[0].lower(), a[0].lower()) in pairs:
            return "dependency"
        if infra_cell[a] and infra_cell[b]:
            ra, rb = rack_of_cell[a].most_common(1)[0][0], rack_of_cell[b].most_common(1)[0][0]
            if ra == rb and ra.strip("/"):
                return "same_rack"
        return None

    cells = sorted(hot)
    parent = {c: c for c in cells}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    why: dict[tuple, list] = defaultdict(list)
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            r = related(a, b)
            if r:
                why[(a, b)].append(r)
                parent[find(a)] = find(b)
    comps: dict[tuple, list] = defaultdict(list)
    for c in cells:
        comps[find(c)].append(c)
    clusters = []
    assigned: set[int] = set()
    for members in comps.values():
        svcs = {s for s, _ in members}
        b_lo, b_hi = min(b for _, b in members), max(b for _, b in members)
        lo = t0 + timedelta(minutes=b_lo * size - scenario.PAD_MIN)
        hi = t0 + timedelta(minutes=(b_hi + 1) * size + scenario.PAD_MIN)
        hosts = {h for h, s in host_svc.items() if s in svcs}
        obs = [o for o in observations if lo <= o.timestamp <= hi and (o.service in svcs or o.host in hosts) and id(o) not in assigned]
        for o in obs:
            assigned.add(id(o))
        edges = [{"a": a, "b": b, "why": r} for (a, b), rs in why.items() for r in rs if a in members]
        clusters.append({"obs": obs, "cells": sorted(members), "hot": {c: hot[c] for c in members}, "services": sorted(svcs),
                         "span": (lo, hi), "edges": edges})
    clusters.sort(key=lambda c: -len(c["obs"]))
    noise = [o for o in observations if id(o) not in assigned]
    return {"clusters": clusters, "noise": noise, "cells": hot, "t0": t0, "bucket_min": size, "n_buckets": n_buckets}


def prune_background(c: dict, observations: list[Observation]) -> list[Observation]:
    """Inside a cluster, drop (service, alarm_type) pairs that fire at their normal rate: they are background
    chatter that merely happened during the incident. Returns the dropped alarms (they go back to noise)."""
    lo, hi = c["span"]
    span_min = max(1.0, (hi - lo).total_seconds() / 60)
    if not observations:
        return []
    t_first, t_last = min(o.timestamp for o in observations), max(o.timestamp for o in observations)
    total_min = max(1.0, (t_last - t_first).total_seconds() / 60)
    global_rate: Counter = Counter()
    for o in observations:
        global_rate[(o.service, str(o.attributes.get("alarm_type", "")))] += 1
    in_cluster: Counter = Counter()
    for o in c["obs"]:
        in_cluster[(o.service, str(o.attributes.get("alarm_type", "")))] += 1
    keep, dropped = [], []
    for o in c["obs"]:
        key = (o.service, str(o.attributes.get("alarm_type", "")))
        expected = global_rate[key] * span_min / total_min
        elevated = in_cluster[key] >= max(2.0, 2.0 * expected)
        if elevated or (o.severity in ("ERROR", "CRITICAL") and scenario.CAUSE_RANK.get(key[1], 1) > 0):
            keep.append(o)
        else:
            dropped.append(o)
    c["obs"] = keep
    c["pruned"] = len(dropped)
    return dropped


_DEP_PATTERNS = None


def _mentioned(o: Observation, services: set[str]) -> str:
    """Provider named in the message ("X servisine yapilan cagri"), when it is a known service."""
    global _DEP_PATTERNS
    if _DEP_PATTERNS is None:
        import re
        _DEP_PATTERNS = [re.compile(p, re.I) for p in getattr(scenario, "DEP_PATTERNS", [])]
    for pat in _DEP_PATTERNS:
        m = pat.search(o.message)
        if m and m[1].lower() in services and m[1].lower() != (o.service or "").lower():
            return m[1].lower()
    return ""


def extract_slow_burns(result: dict, observations: list[Observation]) -> dict:
    """Slow burn = a cause-type alarm of one service escalating to ERROR+ on >= 2 hosts inside 15 min while the
    service's total never crosses its 3x-median line (memory leak -> gc pressure -> oom risk). Such a chain becomes
    its own cluster: the service's cause-type alarms over the chain, plus the timeouts other services raised
    *against this service* in that span (pulled back out of whichever cluster grabbed them). No dependency-table
    links are used, so a long chain can never bridge two unrelated incidents."""
    from .models import SEV_RANK
    t0, size = result["t0"], result["bucket_min"]
    if not t0:
        return result
    services = {o.service.lower() for o in observations if o.service}
    svc_map = {o.service.lower(): o.service for o in observations if o.service}
    roots = {s for c in result["clusters"] for s in c["services"]}
    by_svc_type: dict[tuple[str, str], list] = defaultdict(list)
    for o in observations:
        typ = str(o.attributes.get("alarm_type", "")).lower()
        if o.service and scenario.CAUSE_RANK.get(typ, 0) >= scenario.ESCALATION_MIN_RANK and SEV_RANK[o.severity] >= 3:
            by_svc_type[(o.service, typ)].append(o)
    chains: dict[str, list] = defaultdict(list)          # service -> escalating (type, alarms)
    for (svc, typ), items in by_svc_type.items():
        items.sort(key=lambda o: o.timestamp)
        for i, o in enumerate(items):
            win = [x for x in items if o.timestamp <= x.timestamp <= o.timestamp + timedelta(minutes=15)]
            if len(win) >= scenario.ESCALATION_MIN and len({x.host for x in win}) >= scenario.ESCALATION_MIN_HOSTS:
                chains[svc].append((typ, items))
                break
    new_clusters = []
    for svc, parts in chains.items():
        # only services that are not already the root/member of a card explain something new
        if any(svc in c["services"] and any(k == svc for k, _ in c["cells"]) for c in result["clusters"]):
            continue
        chain_obs = [o for _, items in parts for o in items]
        lo = min(o.timestamp for o in chain_obs) - timedelta(minutes=scenario.PAD_MIN)
        hi = max(o.timestamp for o in chain_obs) + timedelta(minutes=scenario.PAD_MIN + 10)
        mine = [o for o in observations if lo <= o.timestamp <= hi and (o.service == svc or _mentioned(o, services) == svc.lower())]
        if len(mine) < scenario.MIN_CLUSTER_ALARMS:
            continue
        ids = {id(o) for o in mine}
        for c in result["clusters"]:                    # take these alarms back from other clusters / noise
            c["obs"] = [o for o in c["obs"] if id(o) not in ids]
            c["services"] = sorted({o.service for o in c["obs"] if o.service})
        result["noise"] = [o for o in result["noise"] if id(o) not in ids]
        cells = sorted({(svc, _bucket(o, t0, size)) for o in chain_obs})
        hot = {c: {"count": sum(1 for o in mine if o.service == svc and _bucket(o, t0, size) == c[1]), "median": None, "threshold": None,
                   "by": "escalation " + "/".join(sorted({t for t, _ in parts}))} for c in cells}
        result["cells"].update(hot)
        new_clusters.append({"obs": mine, "cells": cells, "hot": hot, "services": sorted({o.service for o in mine if o.service}),
                             "span": (lo, hi), "edges": [], "slow_burn": True})
    result["clusters"] = [c for c in result["clusters"] if c["obs"]] + new_clusters
    result["clusters"].sort(key=lambda c: -len(c["obs"]))
    return result
