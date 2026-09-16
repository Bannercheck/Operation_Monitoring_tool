"""Deterministic engine: fingerprint -> signals (burst) -> correlation -> incidents (root cause, score, rationale).

Works without any LLM. `llm_prompt()` builds an evidence bundle for optional enrichment (paste into Claude SAKA).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import timedelta

from .models import SEV_RANK, Factor, Incident, Observation, Signal
from . import scenario, storm, tables
from .i18n import link_text, narrative_text, reason_text

# ---------------------------------------------------------------- fingerprint
MASKS = [  # (pattern, replacement); applied as ONE combined regex, leftmost-first, so order = priority
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<ip>"),
    (r"\b[0-9a-f]{16,}\b", "<hex>"),
    (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<ts>"),
    (r"https?://\S+", "<url>"),
    (r"(?<=[\s=:/])/[\w./\-]+", "<path>"),
    (r"\b\d+(?:\.\d+)?\s?(?P<unit>ms|s|sec|m|h|%|kb|mb|gb)\b", "<n>{unit}"),
    (r"\b\d+\b", "<n>"),
]
_mask_cache: dict[int, tuple] = {}


def _mask_re():
    """Combined mask regex (+ scenario.EXTRA_MASKS), rebuilt only when the scenario list changes."""
    key = len(scenario.EXTRA_MASKS)
    if key not in _mask_cache:
        pairs = MASKS + list(scenario.EXTRA_MASKS)
        rx = re.compile("|".join(f"(?P<m{i}>{pat})" for i, (pat, _) in enumerate(pairs)), re.I)
        reps = {f"m{i}": rep for i, (_, rep) in enumerate(pairs)}
        _mask_cache[key] = (rx, reps)
    return _mask_cache[key]


def _mask_sub(m: re.Match, reps: dict) -> str:
    rep = reps[m.lastgroup]
    return rep.replace("{unit}", m.group("unit") or "") if "{unit}" in rep else rep


ENTITY_RE = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z][\w\-]*(?:-\d+|\.[a-z]+)+|[a-z]+-(?:api|db|svc|service|cache|queue|worker|gateway|proxy)\b)", re.I)
ENTITY_SAMPLE = 40
DEPENDENCY_WORDS = ["database", "db", "postgres", "mysql", "redis", "kafka", "queue", "connection", "network", "dns",
                    "disk", "storage", "latency", "duration", "slow", "certificate", "auth", "gateway", "no space", "oom"]
WINDOW_MIN = 5
MIN_SEVERITY = "WARN"
RESTART_RE = re.compile(r"\b(restart(?:ed|ing)?|reboot(?:ed)?|start(?:ed|ing)? (?:up|service|server|application)|(?:database|system) (?:is )?ready|"
                        r"listening on|boot(?:ed|ing)?|initializ(?:ed|ing)|reloaded|recovered|back online|healthy|failover complete|"
                        r"shut(?:ting)? down|pool (?:reset|recreated)|reconnected)\b", re.I)
QUIET_MIN = 2   # minutes without errors after the last error to call an incident recovered
WEIGHTS = {"burst": 0.35, "severity": 0.25, "blast_radius": 0.25, "duration": 0.15}


_WS = re.compile(r"\s+")


def template_of(message: str) -> str:
    rx, reps = _mask_re()
    t = rx.sub(lambda m: _mask_sub(m, reps), message.strip())
    return _WS.sub(" ", t).lower()[:200]


DEP_PATTERNS = [re.compile(p, re.I) for p in getattr(scenario, "DEP_PATTERNS", [])]


def entities_of(o: Observation) -> set[str]:
    ents = {x.lower() for x in ENTITY_RE.findall(o.message)}
    for pat in DEP_PATTERNS:
        m = pat.search(o.message)
        if m:
            ents.add(m[1].lower())
    ents.update(x for x in (o.service.lower(), o.host.lower()) if x)
    for k, v in o.attributes.items():
        if isinstance(v, str) and re.fullmatch(r"[\w.\-]{3,40}", v) and \
                any(h in k.lower() for h in ("id", "host", "service", "node", "ip", "target", "endpoint", "db", "pod")):
            ents.add(v.lower())
    return ents


def fingerprint(observations: list[Observation]) -> None:
    for o in observations:
        o.template = template_of(o.message)
        o.fingerprint = hashlib.sha1(f"{o.template}|{o.severity}|{o.service.lower()}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- signals + burst
def build_signals(observations: list[Observation]) -> list[Signal]:
    span_min = 1
    if observations:
        span_min = max(1, int((observations[-1].timestamp - observations[0].timestamp).total_seconds() // 60) + 1)
    groups: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        groups[o.fingerprint].append(o)
    signals = []
    for n, (fp, obs) in enumerate(groups.items(), 1):
        obs.sort(key=lambda o: o.timestamp)
        ents: set[str] = set()
        for o in obs[:ENTITY_SAMPLE]:  # entities are stable within a fingerprint; a sample is enough
            ents |= entities_of(o)
        sig = Signal(id=f"S{n}", fingerprint=fp, template=obs[0].template, severity=obs[0].severity, count=len(obs),
                     services=sorted({o.service for o in obs if o.service}), hosts=sorted({o.host for o in obs if o.host}),
                     entities=ents, first_seen=obs[0].timestamp, last_seen=obs[-1].timestamp, onset=obs[0].timestamp,
                     observations=obs)
        score_burst(sig, span_min)
        sig.span_share = round(((obs[-1].timestamp - obs[0].timestamp).total_seconds() / 60 + 1) / span_min, 3)
        racks = {str(o.attributes.get("tags.kabin") or o.attributes.get("kabin") or "") for o in obs[:ENTITY_SAMPLE]}
        dcs = {str(o.attributes.get("tags.veri_merkezi") or o.attributes.get("veri_merkezi") or "") for o in obs[:ENTITY_SAMPLE]}
        if len(racks) == 1 and len(dcs) == 1 and racks != {""}:
            sig.entities.add(f"rack:{dcs.pop()}/{racks.pop()}")      # whole signal sits in one rack: rack-level incidents link on it
        signals.append(sig)
    return signals


def score_burst(sig: Signal, span_min: int) -> None:
    """Peak events/min vs baseline = median rate over the whole dataset span (quiet minutes count as 0).

    Steady background traffic: baseline ~= peak -> burst 0. Silent-then-spike: baseline 0 -> burst high.
    """
    per_min = Counter(o.timestamp.replace(second=0, microsecond=0) for o in sig.observations)
    rates = sorted(list(per_min.values()) + [0] * max(0, span_min - len(per_min)))
    sig.peak_rate = float(rates[-1])
    sig.baseline_rate = float(rates[len(rates) // 2])
    peak_minute = max(per_min, key=per_min.get)
    threshold = max(1.0, sig.baseline_rate * 2)
    sig.onset = min(m for m, c in per_min.items() if c >= threshold or m == peak_minute)
    ratio = sig.peak_rate / (sig.baseline_rate + 1.0)
    sig.burst_score = round(min(1.0, (ratio - 1) / 9), 3) if ratio > 1 else 0.0


# ---------------------------------------------------------------- correlation
def span_share(s: Signal) -> float:
    """Share of the dataset window this signal stays active in (set by build_signals)."""
    return getattr(s, "span_share", 0.0)


def chronic(s: Signal) -> bool:
    """Steady chatter over most of the window with no burst: background, not an event (even when ERROR)."""
    return span_share(s) >= scenario.CHRONIC_SPAN and s.burst_score < 0.3 and s.count >= 8


def sustained(s: Signal) -> bool:
    """Slow-burn candidate: WARN, many alarms, confined to one service, not chronic chatter."""
    return s.severity == "WARN" and s.count >= scenario.SUSTAINED_MIN and len(s.hosts) <= 2 and not chronic(s)


def interesting(s: Signal) -> bool:
    if chronic(s):
        return False
    min_sev = scenario.MIN_SEVERITY or MIN_SEVERITY
    return SEV_RANK[s.severity] >= SEV_RANK[min_sev] or s.burst_score >= 0.5 or sustained(s)


def dep_pairs(deps: list[dict] | None) -> set[tuple[str, str]]:
    return {(d["source"].lower(), d["target"].lower()) for d in (deps or [])}


def dep_link(a: Signal, b: Signal, pairs: set[tuple[str, str]]) -> tuple[str, str] | None:
    """(dependent, provider) if a service of one signal depends on a service of the other (either direction)."""
    for x in a.services:
        for y in b.services:
            if (x.lower(), y.lower()) in pairs:
                return (x, y)
            if (y.lower(), x.lower()) in pairs:
                return (y, x)
    return None


def correlate(signals: list[Signal], window_min: int | None = None, deps: list[dict] | None = None) -> list[tuple[list[Signal], list[dict]]]:
    """Union-find over signals. Edge = onsets within the window AND (shared entity OR declared dependency OR both bursting)."""
    window_min = window_min or scenario.WINDOW_MIN or WINDOW_MIN
    pairs = dep_pairs(deps)
    cand = [s for s in signals if interesting(s)]
    parent = {s.id: s.id for s in cand}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges: list[dict] = []
    win = timedelta(minutes=window_min)
    for i, a in enumerate(cand):
        for b in cand[i + 1:]:
            # time link: activity intervals overlap or sit within the window of each other (slow burns stay linkable)
            lo, hi = max(a.first_seen, b.first_seen), min(a.last_seen, b.last_seen)
            if lo - hi > win:
                continue
            shared = a.entities & b.entities
            gap = abs((a.onset - b.onset).total_seconds())
            edge = None
            dl = dep_link(a, b, pairs) if pairs else None
            if shared:
                edge = {"a": a.id, "b": b.id, "ents": sorted(shared)[:3], "gap": f"{gap:.0f}"}
            elif dl:
                edge = {"a": a.id, "b": b.id, "ents": [], "dep": dl, "gap": f"{gap:.0f}"}
            elif scenario.LINK_BURST_ONLY and a.burst_score >= 0.5 and b.burst_score >= 0.5:
                edge = {"a": a.id, "b": b.id, "ents": [], "gap": f"{gap:.0f}"}
            if edge:
                edge["why"] = link_text(edge, "en")
                edges.append(edge)
                parent[find(a.id)] = find(b.id)
    comps: dict[str, list[Signal]] = defaultdict(list)
    for s in cand:
        comps[find(s.id)].append(s)
    out = []
    for members in comps.values():
        ids = {s.id for s in members}
        out.append((sorted(members, key=lambda s: s.onset), [e for e in edges if e["a"] in ids]))
    return out


def pick_root_cause(members: list[Signal], deps: list[dict] | None = None, density: bool = False) -> tuple[Signal, list, list]:
    """Earliest onset weighs most; then being a provider others depend on, dependency words, fan-out, severity.

    Returns (root, reason codes, alternatives) where alternatives are the runner-up hypotheses with their own codes.
    """
    words = DEPENDENCY_WORDS + scenario.EXTRA_DEPENDENCY_WORDS
    pairs = dep_pairs(deps)
    earliest = members[0].onset
    # storm helpers: the first cause-type alarm of the group, and a rack-wide network event (many hosts of one rack)
    cause_onsets = [m.onset for m in members if scenario.CAUSE_RANK.get(str(m.observations[0].attributes.get("alarm_type", "")).lower(), 0) >= 2] if density else []
    first_cause_onset = min(cause_onsets) if cause_onsets else None
    rack_root = None
    if density:
        rack_hosts: dict[str, set] = defaultdict(set)
        rack_sigs: dict[str, list] = defaultdict(list)
        for m in members:
            o0 = m.observations[0]
            if str(o0.attributes.get("alarm_type", "")).lower() in scenario.INFRA_TYPES and str(o0.attributes.get("alarm_type", "")).lower() != "ntp_drift":
                rack = f"{o0.attributes.get('tags.veri_merkezi') or o0.attributes.get('veri_merkezi') or ''}/{o0.attributes.get('tags.kabin') or o0.attributes.get('kabin') or ''}"
                if rack.strip("/"):
                    rack_hosts[rack] |= set(m.hosts)
                    rack_sigs[rack].append(m.id)
        if rack_hosts:
            rack, hosts = max(rack_hosts.items(), key=lambda kv: len(kv[1]))
            if len(hosts) >= scenario.RACK_MIN_HOSTS:
                rack_root = {"rack": rack, "hosts": len(hosts), "signals": set(rack_sigs[rack])}
    ranked = []
    for s in members:
        lead_min = (s.onset - earliest).total_seconds() / 60
        dep = int(any(w in s.template for w in words))
        fan = sum(1 for o in members if o is not s and (o.entities & s.entities))
        # provider: how many other members' services depend on one of this signal's services (they suffer when it breaks)
        provider = sum(1 for o in members if o is not s and any((y.lower(), x.lower()) in pairs for x in s.services for y in o.services))
        consumer = sum(1 for o in members if o is not s and any((x.lower(), y.lower()) in pairs for x in s.services for y in o.services))
        atype = str(s.observations[0].attributes.get("alarm_type", "")).lower() if s.observations else ""
        cause = scenario.CAUSE_RANK.get(atype, None)
        if cause is None:
            cause = 0.0
        if density:
            # storm mode: what kind of alarm it is matters more than who fired first (noise fires first, too)
            first_cause = 1.5 if cause >= 2 and s.onset == first_cause_onset else 0.0
            rack_bonus = 2.5 if rack_root and s.id in rack_root["signals"] else 0.0
            score = (cause * 1.2 + SEV_RANK[s.severity] * 0.8 + math.log1p(s.count) * 0.6 - lead_min * 0.4 + dep * 0.5
                     + min(provider, 3) * 0.6 - min(consumer, 3) * 0.6 + fan * 0.05 + first_cause + rack_bonus)
            if first_cause:
                codes_extra = ["r_first_cause"]
            else:
                codes_extra = []
            if rack_bonus:
                codes_extra.append(("r_rack", f"{rack_root['rack']} ({rack_root['hosts']} host)"))
        else:
            codes_extra = []
            score = -lead_min * 3 + dep * 1.5 + fan * 0.3 + SEV_RANK[s.severity] * 0.3 + provider * 2.0 - consumer * 1.0
        codes: list = ["r_earliest"] if lead_min == 0 else [("r_lead", f"{lead_min:.0f}")]
        codes += codes_extra
        if cause >= 3:
            codes.append(("r_cause", atype))
        if provider:
            codes.append(("r_provider", provider))
        if consumer and not provider:
            codes.append(("r_consumer", consumer))
        if dep:
            codes.append("r_dep")
        if fan:
            codes.append(("r_fan", fan))
        ranked.append((round(score, 2), s, codes))
    ranked.sort(key=lambda r: -r[0])
    _, s, codes = ranked[0]
    alts = [{"signal": r[1].id, "score": r[0], "codes": r[2], "template": r[1].template, "services": r[1].services} for r in ranked[1:4]]
    if rack_root:
        codes.append(("r_rack_hint", rack_root["rack"])) if not any(isinstance(c, tuple) and c[0] == "r_rack" for c in codes) else None
    return s, codes, alts


# ---------------------------------------------------------------- scoring + explanation
def build_incident(n: int, members: list[Signal], edges: list[dict], deps: list[dict] | None = None, density: bool = False) -> Incident:
    weights = scenario.WEIGHTS or WEIGHTS
    root, codes, alts = pick_root_cause(members, deps, density)
    why = reason_text(codes, "en")
    services = sorted({x for s in members for x in s.services})
    hosts = sorted({x for s in members for x in s.hosts})
    start, end = min(s.first_seen for s in members), max(s.last_seen for s in members)
    total = sum(s.count for s in members)
    raw = {"burst": max(s.burst_score for s in members),
           "severity": sum(s.count for s in members if SEV_RANK[s.severity] >= 3) / total,
           "blast_radius": min(1.0, (len(services) + len(hosts)) / 6),
           "duration": min(1.0, (end - start).total_seconds() / 3600)}
    labels = {"burst": f"peak {max(s.peak_rate for s in members):.0f}/min vs baseline {max(s.baseline_rate for s in members):.0f}/min",
              "severity": f"{raw['severity']:.0%} of {total} events are ERROR+",
              "blast_radius": f"{len(services)} service(s), {len(hosts)} host(s)",
              "duration": f"{(end - start).total_seconds() / 60:.0f} min"}
    data = {"burst": {"peak": f"{max(s.peak_rate for s in members):.0f}", "base": f"{max(s.baseline_rate for s in members):.0f}"},
            "severity": {"share": f"{raw['severity']:.0%}", "total": total},
            "blast_radius": {"services": len(services), "hosts": len(hosts)},
            "duration": {"minutes": f"{(end - start).total_seconds() / 60:.0f}"}}
    factors = [Factor(k, weights[k], labels[k], round(weights[k] * raw[k], 3), data[k]) for k in weights]
    score = round(sum(f.contribution for f in factors), 3)
    top_sev = max((s.severity for s in members), key=lambda x: SEV_RANK[x])
    severity = "critical" if score >= 0.6 or top_sev == "CRITICAL" else "high" if score >= 0.4 else "medium" if score >= 0.2 else "low"
    timeline = [{"time": s.onset.isoformat(), "signal": s.id, "severity": s.severity, "count": s.count, "template": s.template,
                 "services": s.services, "role": "root cause" if s is root else "symptom"} for s in members]
    evidence = [ref for s in members for ref in s.evidence[:3]]
    recs = [r for key, lst in scenario.RECOMMENDATIONS.items() if key in root.template for r in lst][:4] or \
           ["Investigate the root-cause signal's evidence lines", "Confirm blast radius with service owners"]
    head = root.template or (root.observations[0].raw or root.observations[0].message or "").strip()[:70] or "(no message)"
    rack = next((c[1] for c in codes if isinstance(c, tuple) and c[0] == "r_rack"), None)
    if rack:
        head = f"{rack.split(' (')[0]} kabin ağ olayı: {head}"
    title = head[:90] + (f" ({', '.join(services[:3])}{'…' if len(services) > 3 else ''})" if services else "")
    inc = Incident(id=f"INC-{n}", title=title, severity=severity, score=score, root_cause_signal=root.id, root_cause_reason=why,
                   affected_services=services, affected_hosts=hosts, started_at=start, ended_at=end,
                   signal_ids=[s.id for s in members], factors=factors, evidence=evidence, timeline=timeline,
                   links=edges, narrative="", recommendations=recs, root_cause_codes=codes, root_cause_alternatives=alts)
    inc.narrative = narrative_text(inc, {s.id: s for s in members}, "en")
    return inc


def explain_incident(inc: Incident, members: list[Signal], observations: list[Observation]) -> None:
    """Fill origin (where), timing (when / how long) and recovery (did it stop by itself, restart evidence)."""
    obs = [o for s in members for o in s.observations]
    files: Counter = Counter(o.source for o in obs)
    inc.origin = {"files": dict(files.most_common()), "services": inc.affected_services, "hosts": inc.affected_hosts,
                  "agents": sorted({str(o.attributes.get("agent")) for o in obs if o.attributes.get("agent")}),
                  "parsers": sorted({o.parser for o in obs}), "kinds": sorted({o.kind for o in obs}),
                  "environments": dict(Counter(o.environment or "unknown" for o in obs).most_common()),
                  "origins": dict(Counter(o.origin for o in obs if o.origin).most_common(5))}
    errors = sorted(o.timestamp for o in obs if SEV_RANK[o.severity] >= 3) or sorted(o.timestamp for o in obs)
    first_signal, last_error = inc.started_at, errors[-1]
    dataset_end = observations[-1].timestamp if observations else inc.ended_at
    quiet = (dataset_end - last_error).total_seconds()
    inc.timing = {"first_signal": first_signal.isoformat(), "last_error": last_error.isoformat(),
                  "duration_s": round((last_error - first_signal).total_seconds()), "quiet_s": round(quiet), "dataset_end": dataset_end.isoformat(),
                  "error_count": len(errors)}
    ents = {x.lower() for x in inc.affected_services + inc.affected_hosts}
    lo, hi = first_signal - timedelta(minutes=1), last_error + timedelta(minutes=10)
    window = [o for o in observations if lo <= o.timestamp <= hi and (o.service.lower() in ents or o.host.lower() in ents or not ents)]
    restart = next((o for o in window if o.timestamp >= first_signal and RESTART_RE.search(o.message)), None)
    normal_after = next((o for o in observations if o.timestamp > last_error and SEV_RANK[o.severity] < 2
                         and (o.service.lower() in ents or o.host.lower() in ents)), None)
    if quiet < QUIET_MIN * 60:
        kind = "ongoing"
    elif restart:
        kind = "restart"
    elif normal_after:
        kind = "self_healed"
    else:
        kind = "stopped"   # errors stopped, but no evidence of normal traffic or a restart
    inc.recovery = {"kind": kind, "last_error": last_error.isoformat(),
                    "recovered_at": (restart or normal_after).timestamp.isoformat() if (restart or normal_after) else None,
                    "evidence": (restart or normal_after).ref if (restart or normal_after) else None,
                    "what": (restart.message[:160] if restart else (normal_after.message[:160] if normal_after else "")),
                    "quiet_min": round(quiet / 60, 1)}


def postmortem_md(inc: Incident, signals: dict[str, Signal]) -> str:
    out = [f"# Postmortem {inc.id}: {inc.title}", "", f"- Severity: **{inc.severity}** (score {inc.score})",
           f"- Window: {inc.started_at.isoformat()} -> {inc.ended_at.isoformat()}",
           f"- Affected services: {', '.join(inc.affected_services) or '-'}",
           f"- Affected hosts: {', '.join(inc.affected_hosts) or '-'}",
           f"- Root cause candidate: {inc.root_cause_signal} ({inc.root_cause_reason})", "", "## Timeline", ""]
    out += [f"- {t['time'][11:19]} [{t['severity']}] {t['signal']} x{t['count']} {t['template']} ({t['role']})" for t in inc.timeline]
    out += ["", "## Why this decision", ""] + [f"- {f.name}: weight {f.weight}, {f.value}, contribution {f.contribution}" for f in inc.factors]
    out += ["", "## Evidence", ""]
    for sid in inc.signal_ids:
        out += [f"- `{o.ref}` {o.message[:140]}" for o in signals[sid].observations[:3]]
    out += ["", "## Recommendations", ""] + [f"- {r}" for r in inc.recommendations]
    out += ["", "## Narrative", "", inc.narrative, ""]
    return "\n".join(out)


def llm_prompt(inc: Incident, signals: dict[str, Signal]) -> str:
    """Evidence bundle for optional LLM enrichment (paste into Claude SAKA). The answer must cite refs."""
    ev = "\n".join(f"[{o.ref}] {o.timestamp:%H:%M:%S} {o.severity} {o.service} {o.message[:160]}"
                   for sid in inc.signal_ids for o in signals[sid].observations[:5])
    return (f"You are an SRE. Below is evidence for incident {inc.id}. Root cause candidate: {inc.root_cause_signal} "
            f"({inc.root_cause_reason}). Write (1) a 3-sentence summary, (2) the most likely root cause, "
            f"(3) three concrete actions. Cite evidence refs in [brackets]; do not invent facts.\n\nEVIDENCE\n{ev}\n")


# ---------------------------------------------------------------- orchestration
def storm_edges(sigs: list[Signal], c: dict, deps: list[dict]) -> list[dict]:
    """Explain why each signal of a density cluster belongs with the others (root-first star of links)."""
    if not sigs:
        return []
    pairs = dep_pairs(deps)
    root = sigs[0]
    edges = []
    for s in sigs[1:]:
        gap = f"{abs((s.onset - root.onset).total_seconds()):.0f}"
        shared = sorted(root.entities & s.entities)[:3]
        dl = dep_link(root, s, pairs)
        if shared:
            edges.append({"a": root.id, "b": s.id, "ents": shared, "gap": gap})
        elif dl:
            edges.append({"a": root.id, "b": s.id, "ents": [], "dep": dl, "gap": gap})
        else:
            edges.append({"a": root.id, "b": s.id, "ents": [], "gap": gap, "cell": True})
        edges[-1]["why"] = link_text(edges[-1], "en")
    return edges


class Analysis:
    def __init__(self, observations: list[Observation], report: list[dict]):
        self.observations, self.report = observations, report
        self.dependencies = tables.dependencies(report)          # declared service dependencies (may be empty)
        self.inventory = tables.inventory(report)                # host -> dc / rack / env / criticality (may be empty)
        if not self.dependencies:                                # enriched single file: columns carry the graph and the inventory
            d2, i2 = tables.from_observations(observations)
            self.dependencies = self.dependencies or d2
            self.inventory = self.inventory or i2
        fingerprint(observations)
        mode = scenario.CLUSTERING
        self.mode = "density" if mode == "density" or (mode == "auto" and self.dependencies) else "fingerprint"
        self.storm: dict = {}
        self.noise_obs: list[Observation] = []
        if self.mode == "density":
            self.storm = storm.cluster(observations, self.dependencies, self.inventory)
            self.noise_obs = self.storm["noise"]
            comps, all_sigs, n = [], [], 0
            for c in self.storm["clusters"]:
                if scenario.PRUNE_BACKGROUND:
                    self.noise_obs += storm.prune_background(c, observations)
                if len(c["obs"]) < scenario.MIN_CLUSTER_ALARMS or sum(1 for o in c["obs"] if SEV_RANK[o.severity] >= 3) < scenario.MIN_CLUSTER_ERRORS:
                    c["small"] = True
                sigs = sorted(build_signals(c["obs"]), key=lambda s: (s.first_seen, -SEV_RANK[s.severity]))
                for sg in sigs:
                    n += 1
                    sg.id = f"S{n}"
                all_sigs += sigs
                if sigs:
                    comps.append((sigs, storm_edges(sigs, c, self.dependencies), c.get("small", False)))
            noise_sigs = build_signals(self.noise_obs)
            for sg in noise_sigs:
                n += 1
                sg.id = f"S{n}"
            self.signals = sorted(all_sigs + noise_sigs, key=lambda s: (-s.burst_score, -SEV_RANK[s.severity], -s.count))
        else:
            self.signals = sorted(build_signals(observations), key=lambda s: (-s.burst_score, -SEV_RANK[s.severity], -s.count))
            comps = [(m, e, False) for m, e in correlate(self.signals, deps=self.dependencies)
                     if len(m) > 1 or SEV_RANK[m[0].severity] >= 3 or m[0].burst_score >= 0.5]
        built = [(build_incident(i, m, e, self.dependencies, self.mode == "density"), small) for i, (m, e, small) in enumerate(comps, 1)]
        incs = sorted((inc for inc, small in built if not small), key=lambda i: -i.score)
        small_incs = [inc for inc, small in built if small]
        cap = scenario.MAX_INCIDENTS or len(incs)
        self.demoted = incs[cap:] + small_incs                   # beyond the card budget or too small: audit, not cards
        incs = incs[:cap]
        self.small_ids: set[str] = set()
        sig_by_id = {s.id: s for s in self.signals}
        for n, inc in enumerate(incs, 1):
            inc.id = f"INC-{n}"
            explain_incident(inc, [sig_by_id[x] for x in inc.signal_ids], observations)
        for n, inc in enumerate(self.demoted, 1):
            inc.id = f"LOW-{n}"
            if inc in small_incs:
                self.small_ids.add(inc.id)
        self.incidents = incs
        self.signal_by_id = {s.id: s for s in self.signals}
        self.incident_by_id = {i.id: i for i in self.incidents}

        self.obs_by_ref = {o.ref: o for o in observations}

    def noise_audit(self) -> dict:
        """Why each alarm that is not on a card was left out. Rows per signal, totals per reason."""
        in_card = {sid for i in self.incidents for sid in i.signal_ids}
        demoted = {sid: i.id for i in self.demoted for sid in i.signal_ids}
        rows = []
        for s in self.signals:
            if s.id in in_card:
                continue
            if s.id in demoted:
                reason = "n_small" if demoted[s.id] in self.small_ids else "n_demoted"
            elif self.mode == "density":
                reason = "n_baseline"                            # inside its service's normal alarm rate: no hot cell
            elif not interesting(s):
                reason = "n_low"                                 # below WARN and no burst: background chatter
            else:
                reason = "n_isolated"                            # interesting but linked to nothing in its window
            rows.append({"signal": s.id, "reason": reason, "group": demoted.get(s.id, ""), "severity": s.severity, "count": s.count,
                         "burst": round(s.burst_score, 2), "services": ", ".join(s.services[:3]), "hosts": len(s.hosts),
                         "first": s.first_seen, "last": s.last_seen, "template": s.template})
        totals = Counter()
        for r in rows:
            totals[r["reason"]] += r["count"]
        on_cards = sum(i.total_events if hasattr(i, "total_events") else sum(self.signal_by_id[x].count for x in i.signal_ids) for i in self.incidents)
        return {"rows": rows, "totals": dict(totals), "eliminated": sum(totals.values()), "on_cards": on_cards, "total": len(self.observations)}

    def funnel(self) -> dict:
        return {"raw_events": len(self.observations), "fingerprints": len(self.signals),
                "meaningful_signals": sum(1 for s in self.signals if interesting(s)), "incidents": len(self.incidents),
                "reduction": round(len(self.observations) / max(len(self.signals), 1), 1)}


def signal_dict(s: Signal) -> dict:
    return {"id": s.id, "severity": s.severity, "template": s.template, "count": s.count, "burst": s.burst_score,
            "peak/min": s.peak_rate, "base/min": s.baseline_rate, "services": ", ".join(s.services),
            "hosts": ", ".join(s.hosts), "onset": s.onset.strftime("%H:%M:%S")}


def incident_dict(inc: Incident) -> dict:
    d = asdict(inc)
    d["started_at"], d["ended_at"] = inc.started_at.isoformat(), inc.ended_at.isoformat()
    return d


def suggested_owner(inc: Incident, root: Signal) -> str:
    """Owner of the card's first action, from scenario.OWNERS by root service / rack hint."""
    key = " ".join(root.services).lower() + (" rack" if any(isinstance(c, tuple) and c[0] == "r_rack" for c in inc.root_cause_codes) else "")
    for sub, owner in scenario.OWNERS.items():
        if sub in key:
            return owner
    return scenario.DEFAULT_OWNER
