"""CLI:  watchover <path>            profile + analysis summary
        watchover <path> --inspect  per-file format / roles / columns (for the first 10 minutes with a new dataset)
        watchover <path> --json     full machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

from .analysis import Analysis, incident_dict, signal_dict
from .format_detector import detect_format
from .loader import iter_path
from .normalize import auto_map, normalize_severity, parse_timestamp
from .parsers import PARSERS
from .pipeline import ingest_path
from .profiler import profile, profile_text


def inspect(path: str, sample_lines: int = 5) -> None:
    for fname, text in iter_path(path):
        lines = text.splitlines()
        fmt, conf = detect_format(text)
        recs = list(PARSERS[fmt].records(text))
        keys: list[str] = []
        filled: Counter = Counter()
        examples: dict[str, Counter] = defaultdict(Counter)
        for _, r in recs:
            for k, v in r.items():
                if k not in keys:
                    keys.append(k)
                if v not in (None, ""):
                    filled[k] += 1
                    examples[k][str(v)[:60]] += 1
        roles = auto_map(keys, [r for _, r in recs[:50]])
        stamps = sorted(t for t in (parse_timestamp(r.get(roles["timestamp"])) for _, r in recs) if t) if roles["timestamp"] else []
        print("=" * 78)
        print(f"FILE     {fname}\nFORMAT   {fmt} (confidence {conf:.2f})\nSIZE     {len(lines)} lines, {len(recs)} records")
        print("ROLES    " + ", ".join(f"{k}={v or '?'}" for k, v in roles.items()))
        if stamps:
            span = stamps[-1] - stamps[0]
            print(f"TIME     {stamps[0].isoformat()} -> {stamps[-1].isoformat()}  span {span}  ~{len(stamps) / max(span.total_seconds() / 60, 1):.1f}/min")
        if roles["severity"]:
            print("SEVERITY " + ", ".join(f"{k}:{v}" for k, v in Counter(normalize_severity(r.get(roles["severity"])) for _, r in recs).most_common()))
        print("COLUMNS")
        for k in keys[:40]:
            top = ", ".join(f"{v} ({c})" for v, c in examples[k].most_common(3))
            print(f"  {k:<30} fill {filled[k] / max(len(recs), 1):>5.2f}  distinct {len(examples[k]):>6}  {top[:70]}")
        print("SAMPLE")
        for ln in [x for x in lines if x.strip()][:sample_lines]:
            print("  " + ln[:160])
    print("=" * 78)


def summary(a: Analysis, prof: dict) -> str:
    f = a.funnel()
    out = [profile_text(prof), "",
           f"{f['raw_events']} raw events -> {f['fingerprints']} fingerprints -> {f['meaningful_signals']} meaningful signals -> {f['incidents']} incidents", ""]
    for inc in a.incidents:
        out.append(f"{inc.id} [{inc.severity}] score {inc.score}: {inc.title}")
        out.append("  " + inc.narrative)
        out += [f"  {t['time'][11:19]} {t['signal']} [{t['severity']}] x{t['count']} {t['template'][:80]} ({t['role']})" for t in inc.timeline]
        out.append("  evidence: " + ", ".join(inc.evidence[:8]))
        out.append("  recommendations: " + " | ".join(inc.recommendations))
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lines", type=int, default=5)
    ap.add_argument("--enrich", metavar="OUT", help="write the enriched single-file dataset (.csv or .jsonl) + OUT.summary.json and exit")
    args = ap.parse_args(argv)
    if args.inspect:
        inspect(args.path, args.lines)
        return 0
    obs, report = ingest_path(args.path)
    if args.enrich:
        from pathlib import Path
        from .analysis import Analysis
        from .enrich import enrich, summary, to_csv, to_jsonl
        a = Analysis(obs, report)
        rows = enrich(a)
        out = Path(args.enrich)
        out.write_text(to_jsonl(rows) if out.suffix == ".jsonl" else to_csv(rows), encoding="utf-8")
        out.with_suffix(out.suffix + ".summary.json").write_text(json.dumps(summary(a, rows), ensure_ascii=False, indent=1), encoding="utf-8")
        from .stamp import stamp
        st = stamp(a)
        print(f"{out}: {len(rows)} rows, {len(a.incidents)} incidents; summary -> {out.with_suffix(out.suffix + '.summary.json')}")
        print(f"watchover v{st['version']} · engine {st['engine']} · input {st['input']} · result {st['result']}")
        return 0
    a = Analysis(obs, report)
    prof = profile(obs, report)
    prof.pop("per_minute", None)
    if args.json:
        print(json.dumps({"profile": prof, "funnel": a.funnel(), "signals": [signal_dict(s) for s in a.signals],
                          "incidents": [incident_dict(i) for i in a.incidents]}, indent=2, default=str))
    else:
        from .stamp import stamp
        st = stamp(a)
        print(summary(a, prof))
        print(f"\nwatchover v{st['version']} · engine {st['engine']} · git {st['git']} · input {st['input']} · result {st['result']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
