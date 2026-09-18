"""Compare two analysed datasets without mixing them: KPI deltas, severity mix, shared / unique signals, incidents."""

from __future__ import annotations

from collections import Counter

from .analysis import Analysis, interesting
from .models import SEV_RANK


def kpis(a: Analysis, prof: dict) -> dict:
    f = a.funnel()
    n = len(a.observations)
    errors = sum(1 for o in a.observations if SEV_RANK[o.severity] >= 3)
    return {"raw_events": n, "fingerprints": f["fingerprints"], "meaningful": f["meaningful_signals"], "incidents": f["incidents"],
            "reduction": f["reduction"], "error_share": round(errors / n, 4) if n else 0.0,
            "services": len(prof.get("services", [])), "hosts": len(prof.get("hosts", [])), "error_classes": prof.get("error_classes", 0),
            "minutes": prof.get("time_range", {}).get("minutes", 0), "files": len(prof.get("files", []))}


def compare(a: Analysis, pa: dict, b: Analysis, pb: dict) -> dict:
    ka, kb = kpis(a, pa), kpis(b, pb)
    rows = []
    for k in ka:
        va, vb = ka[k], kb[k]
        delta = (vb - va) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        rows.append({"metric": k, "a": va, "b": vb, "delta": round(delta, 4) if delta is not None else None,
                     "delta_pct": round(100 * delta / va, 1) if delta is not None and va else None})
    sev = [{"dataset": "A", "severity": s, "events": c} for s, c in Counter(o.severity for o in a.observations).items()] + \
          [{"dataset": "B", "severity": s, "events": c} for s, c in Counter(o.severity for o in b.observations).items()]
    ta = {s.template: s for s in a.signals}
    tb = {s.template: s for s in b.signals}
    shared = [{"template": tpl, "severity": ta[tpl].severity, "count_a": ta[tpl].count, "count_b": tb[tpl].count,
               "burst_a": ta[tpl].burst_score, "burst_b": tb[tpl].burst_score, "delta": tb[tpl].count - ta[tpl].count}
              for tpl in ta.keys() & tb.keys()]
    shared.sort(key=lambda r: -abs(r["delta"]))
    only_a = sorted([{"template": tpl, "severity": s.severity, "count": s.count, "burst": s.burst_score, "meaningful": interesting(s)} for tpl, s in ta.items() if tpl not in tb],
                    key=lambda r: (-r["meaningful"], -r["count"]))
    only_b = sorted([{"template": tpl, "severity": s.severity, "count": s.count, "burst": s.burst_score, "meaningful": interesting(s)} for tpl, s in tb.items() if tpl not in ta],
                    key=lambda r: (-r["meaningful"], -r["count"]))
    inc = lambda x: [{"id": i.id, "severity": i.severity, "score": i.score, "title": i.title, "services": ", ".join(i.affected_services),  # noqa: E731
                      "window": f"{i.started_at:%H:%M}–{i.ended_at:%H:%M}"} for i in x.incidents]
    # relative-minute timelines so datasets from different days can be overlaid
    def rel(x: Analysis, label: str):
        if not x.observations:
            return []
        t0 = x.observations[0].timestamp
        c = Counter(int((o.timestamp - t0).total_seconds() // 60) for o in x.observations)
        return [{"dataset": label, "minute": m, "events": v} for m, v in sorted(c.items())]
    return {"kpis": rows, "severity": sev, "shared": shared, "only_a": only_a, "only_b": only_b,
            "incidents_a": inc(a), "incidents_b": inc(b), "timeline": rel(a, "A") + rel(b, "B"),
            "summary": {"shared": len(shared), "only_a": len(only_a), "only_b": len(only_b)}}
