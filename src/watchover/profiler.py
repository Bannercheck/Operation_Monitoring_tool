"""Dataset profiler: what did we just receive? Files, records, probable sources, entities, time range, relations."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import pandas as pd

from .models import Observation

ID_LIKE = re.compile(r"(^|[_.])(id|key|name|hostname|service|host|node|pod)$", re.I)


def bucket_for(span) -> str:
    """Time bucket that keeps charts to a few hundred bars: minutes up to 6h, then 5min/1h/1D."""
    minutes = span.total_seconds() / 60
    return "1min" if minutes <= 360 else "5min" if minutes <= 2880 else "1h" if minutes <= 60 * 24 * 90 else "1D"


def profile(observations: list[Observation], report: list[dict]) -> dict:
    if not observations:
        return {"files": report, "records": 0}
    df = pd.DataFrame([{"source": o.source, "kind": o.kind, "severity": o.severity, "service": o.service,
                        "host": o.host, "timestamp": o.timestamp} for o in observations])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    per_file = df.groupby("source").agg(records=("kind", "size"), kind=("kind", "first"),
                                        start=("timestamp", "min"), end=("timestamp", "max")).reset_index()
    # columns per file (attributes + roles) for relation suggestions
    cols: dict[str, set[str]] = defaultdict(set)
    values: dict[tuple[str, str], set[str]] = defaultdict(set)
    for o in observations:
        for k, v in o.attributes.items():
            cols[o.source].add(k)
            if isinstance(v, (str, int)) and len(values[(o.source, k)]) < 500:
                values[(o.source, k)].add(str(v))
        for k, v in (("service", o.service), ("host", o.host)):
            if v:
                cols[o.source].add(k); values[(o.source, k)].add(v)
    relations = []
    files = list(cols)
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            for ka in cols[a]:
                for kb in cols[b]:
                    if not (ID_LIKE.search(ka) or ID_LIKE.search(kb)):
                        continue
                    va, vb = values[(a, ka)], values[(b, kb)]
                    if len(va) >= 2 and len(vb) >= 2:
                        overlap = len(va & vb) / min(len(va), len(vb))
                        if overlap >= 0.5:
                            relations.append({"from": f"{a}.{ka}", "to": f"{b}.{kb}", "overlap": round(overlap, 2)})
    relations.sort(key=lambda r: -r["overlap"])
    error_classes = Counter(o.template or o.message[:40] for o in observations if o.severity in ("ERROR", "CRITICAL"))
    envs = Counter(o.environment or "unknown" for o in observations)
    env_errors = Counter(o.environment or "unknown" for o in observations if o.severity in ("ERROR", "CRITICAL"))
    origins = Counter(o.origin for o in observations if o.origin)
    origin_errors = Counter(o.origin for o in observations if o.origin and o.severity in ("ERROR", "CRITICAL"))
    return {
        "files": [dict(r, start=str(per_file.loc[per_file.source == r["file"], "start"].iloc[0])[:19] if (per_file.source == r["file"]).any() else "-")
                  for r in report],
        "records": len(observations),
        "probable_sources": dict(Counter(o.kind for o in observations)),
        "services": sorted({o.service for o in observations if o.service}),
        "hosts": sorted({o.host for o in observations if o.host}),
        "error_classes": len(error_classes),
        "environments": {e: {"events": n, "errors": env_errors.get(e, 0), "error_rate": round(env_errors.get(e, 0) / n, 3)} for e, n in envs.most_common()},
        "origins": {o_: {"events": n, "errors": origin_errors.get(o_, 0)} for o_, n in origins.most_common(20)},
        "severity": dict(Counter(o.severity for o in observations)),
        "time_range": {"start": df.timestamp.min().isoformat(), "end": df.timestamp.max().isoformat(),
                       "minutes": round((df.timestamp.max() - df.timestamp.min()).total_seconds() / 60, 1)},
        "relations": relations[:10],
        "bucket": bucket_for(df.timestamp.max() - df.timestamp.min()),
        "per_minute": df.set_index("timestamp").resample(bucket_for(df.timestamp.max() - df.timestamp.min())).size().rename("events").reset_index(),
    }


def profile_text(p: dict, lang: str = "en") -> str:
    """The 30-second summary shown right after upload."""
    from .i18n import t
    if not p.get("records"):
        return t("p_none", lang)
    lines = [t("p_found", lang, files=len(p["files"]), records=f"{p['records']:,}"),
             t("p_sources", lang, items=", ".join(f"{k} ({v})" for k, v in p["probable_sources"].items())),
             t("p_entities", lang, services=len(p["services"]), hosts=len(p["hosts"]), errors=p["error_classes"]),
             t("p_range", lang, start=p["time_range"]["start"][11:16], end=p["time_range"]["end"][11:16], minutes=p["time_range"]["minutes"])]
    if p["relations"]:
        lines.append(t("p_relations", lang, items="; ".join(f"{r['from']} -> {r['to']}" for r in p["relations"][:4])))
    return "\n".join(lines)
