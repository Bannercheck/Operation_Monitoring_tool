"""Enriched single-file dataset: every alarm row + inventory + dependency context + what the engine decided.

Why: the raw package spreads meaning over four files. One enriched table answers, per alarm, "where is this
host, who depends on this service, is this alarm a cause or a symptom, which incident is it part of, and if it
was dropped, why". The file is self-contained: the pipeline rebuilds the dependency graph and the inventory
from its columns (tables.from_observations), so uploading only alarms_enriched.csv gives the same analysis.
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict

from . import scenario
from .models import SEV_RANK, Observation

SEV_LABEL_TR = {"CRITICAL": "kritik", "ERROR": "buyuk", "WARN": "kucuk", "INFO": "uyari", "DEBUG": "bilgi"}
COLUMNS = ["alarm_id", "timestamp", "source_system", "host", "service", "severity", "severity_label", "alarm_type", "alarm_class", "cause_rank",
           "message", "mentioned_dependency", "veri_merkezi", "kabin", "ortam", "is_kritikligi", "inventory_service", "inventory_match",
           "depends_on", "dependents", "dependency_criticality", "is_infra", "bucket_start", "bucket_alarms", "service_median", "hot_cell",
           "incident_id", "role", "noise_reason"]


def _attr(o: Observation, *keys: str, default: str = "") -> str:
    for k in keys:
        v = o.attributes.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def enrich(analysis) -> list[dict]:
    """Rows in COLUMNS order, one per observation (all 3.000 alarms, sorted by time)."""
    from .analysis import entities_of
    obs = analysis.observations
    deps = analysis.dependencies or []
    inv = analysis.inventory or {}
    depends_on: dict[str, list[str]] = defaultdict(list)
    dependents: dict[str, list[str]] = defaultdict(list)
    crit: dict[tuple[str, str], str] = {}
    for d in deps:
        depends_on[d["source"]].append(d["target"])
        dependents[d["target"]].append(d["source"])
        crit[(d["source"], d["target"])] = d.get("criticality", "")
    services = {o.service for o in obs if o.service}
    # what the engine decided
    role: dict[str, tuple[str, str]] = {}                # alarm_id -> (incident_id, role)
    for inc in analysis.incidents:
        root = analysis.signal_by_id[inc.root_cause_signal]
        for sid in inc.signal_ids:
            sig = analysis.signal_by_id[sid]
            for o in sig.observations:
                role[o.ref] = (inc.id, "kok_neden" if sig is root else "belirti")
    for inc in getattr(analysis, "demoted", []):
        for sid in inc.signal_ids:
            for o in analysis.signal_by_id[sid].observations:
                role.setdefault(o.ref, (inc.id, "kucuk_grup"))
    noise_reason: dict[str, str] = {}
    for r in analysis.noise_audit()["rows"]:
        for o in analysis.signal_by_id[r["signal"]].observations:
            noise_reason[o.ref] = r["reason"]
    storm = getattr(analysis, "storm", {}) or {}
    t0, bm, cells = storm.get("t0"), storm.get("bucket_min", 5), storm.get("cells", {})
    bucket_counts: dict[tuple[str, int], int] = defaultdict(int)
    if t0:
        for o in obs:
            bucket_counts[(o.service, int((o.timestamp - t0).total_seconds() // (bm * 60)))] += 1
    medians: dict[str, float] = {}
    if t0:
        import statistics
        n_buckets = storm.get("n_buckets", 1)
        for svc in services:
            medians[svc] = statistics.median([bucket_counts.get((svc, i), 0) for i in range(n_buckets)])
    rows = []
    for o in obs:
        atype = _attr(o, "alarm_type").lower()
        cause = scenario.CAUSE_RANK.get(atype)
        inv_row = inv.get(o.host, {})
        dc, rack, env = _attr(o, "tags.veri_merkezi", "veri_merkezi"), _attr(o, "tags.kabin", "kabin"), _attr(o, "tags.ortam", "ortam") or o.environment
        ments = sorted(e for e in entities_of(o) if e in {s.lower() for s in services} and e != (o.service or "").lower())
        b = int((o.timestamp - t0).total_seconds() // (bm * 60)) if t0 else None
        inc_id, r = role.get(o.ref, ("", ""))
        rows.append({
            "alarm_id": _attr(o, "alarm_id"), "timestamp": o.timestamp.strftime("%Y-%m-%dT%H:%M:%S"), "source_system": o.origin or _attr(o, "source_system"),
            "host": o.host, "service": o.service, "severity": _attr(o, "severity_raw") or {v: k for k, v in scenario.SEVERITY_MAP.items()}.get(o.severity, ""),
            "severity_label": SEV_LABEL_TR.get(o.severity, o.severity.lower()), "alarm_type": atype,
            "alarm_class": "neden" if (cause or 0) >= 2 else "belirti" if cause is not None and cause <= 1 else "kaynak" if cause else "arka_plan",
            "cause_rank": "" if cause is None else cause, "message": o.message, "mentioned_dependency": ",".join(ments),
            "veri_merkezi": dc or inv_row.get("dc", ""), "kabin": rack or inv_row.get("rack", ""), "ortam": env or inv_row.get("env", ""),
            "is_kritikligi": inv_row.get("criticality", ""), "inventory_service": inv_row.get("service", ""),
            "inventory_match": "" if not inv_row else ("evet" if inv_row.get("service", "") == o.service and inv_row.get("dc", "") == dc and inv_row.get("rack", "") == rack else "hayir"),
            "depends_on": ",".join(depends_on.get(o.service, [])), "dependents": ",".join(dependents.get(o.service, [])),
            "dependency_criticality": ",".join(f"{t}:{crit[(o.service, t)]}" for t in depends_on.get(o.service, []) if crit.get((o.service, t))),
            "is_infra": "evet" if atype in scenario.INFRA_TYPES else "hayir",
            "bucket_start": (t0.__class__.strftime(t0, "%H:%M") if False else (t0 + __import__("datetime").timedelta(minutes=b * bm)).strftime("%H:%M")) if t0 else "",
            "bucket_alarms": bucket_counts.get((o.service, b), "") if t0 else "", "service_median": medians.get(o.service, "") if t0 else "",
            "hot_cell": ("evet" if (o.service, b) in cells else "hayir") if t0 else "",
            "incident_id": inc_id, "role": r or "gurultu", "noise_reason": noise_reason.get(o.ref, "") if not inc_id else "",
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps({k: r.get(k, "") for k in COLUMNS}, ensure_ascii=False) for r in rows) + "\n"


def summary(analysis, rows: list[dict]) -> dict:
    """Small companion summary: incidents with root cause, chain, owners; totals."""
    from .analysis import suggested_owner
    from .i18n import reason_text
    from .stamp import stamp
    out = {"alarms": len(rows), "incidents": [], "noise": sum(1 for r in rows if r["role"] == "gurultu"),
           "hot_cells": sum(1 for r in rows if r["hot_cell"] == "evet"), "stamp": stamp(analysis)}
    for inc in analysis.incidents:
        root = analysis.signal_by_id[inc.root_cause_signal]
        out["incidents"].append({"id": inc.id, "title": inc.title, "started": inc.started_at.strftime("%H:%M"), "ended": inc.ended_at.strftime("%H:%M"),
                                 "alarms": sum(analysis.signal_by_id[s].count for s in inc.signal_ids), "services": inc.affected_services,
                                 "root_cause": {"signal": root.id, "service": root.services, "template": root.template, "alarm_type": _attr(root.observations[0], "alarm_type"),
                                                "why": reason_text(inc.root_cause_codes, "tr")},
                                 "alternatives": [{"template": x["template"], "services": x["services"], "score": x["score"], "why": reason_text(x["codes"], "tr")} for x in inc.root_cause_alternatives],
                                 "first_action": inc.recommendations[0] if inc.recommendations else "", "owner": suggested_owner(inc, root)})
    return out
