"""Parser coverage: run the production pipeline over samples/corpus (one file per log family) and measure what it extracts.

    python -m watchover.coverage                 table per file + overall score
    python -m watchover.coverage --json          machine readable
    python -m watchover.coverage --update-baseline   accept the current scores (samples/corpus/baseline.json)

Per file: detected format, non-empty lines, events, and the share of events with a timestamp (not filled in from a
neighbour), a level that matches the token in the raw line (lines without a level token are not counted), a host and a
service. `score` is the mean of the four shares. tests/test_coverage.py fails when a file scores below the baseline, so the
parser can only get better; improvements are recorded with --update-baseline.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import scenario
from .pipeline import _parse_one

CORPUS = Path(__file__).resolve().parents[2] / "samples" / "corpus"
BASELINE = CORPUS / "baseline.json"
LEVEL_RE = re.compile(r"(?<![A-Za-z])(TRACE|DEBUG|FINE|INFO|INFORMATION|NOTICE|WARN|WARNING|ERR|ERROR|SEVERE|CRIT|CRITICAL|FATAL|EMERG|EMERGENCY|ALERT|PANIC)(?![A-Za-z])", re.I)
LEVEL_MAP = {"TRACE": "DEBUG", "DEBUG": "DEBUG", "FINE": "DEBUG", "INFO": "INFO", "INFORMATION": "INFO", "NOTICE": "INFO", "WARN": "WARN", "WARNING": "WARN",
             "ERR": "ERROR", "ERROR": "ERROR", "SEVERE": "ERROR", "CRIT": "CRITICAL", "CRITICAL": "CRITICAL", "FATAL": "CRITICAL", "EMERG": "CRITICAL",
             "EMERGENCY": "CRITICAL", "ALERT": "CRITICAL", "PANIC": "CRITICAL"}
FIELDS = ("timestamp", "level", "host", "service")
SAMPLES = Path(__file__).resolve().parents[2] / "samples"
DATASETS = ("demo_mixed.zip", "alarm_storm.zip", "sap_logs.zip")


def measure_file(name: str, text: str) -> dict:
    """Coverage of one file through the real pipeline (format detection, parser, auto-mapping, normalisation)."""
    rows, rep = _parse_one(name, text, scenario.MAPPING or None)
    lines = sum(1 for ln in text.splitlines() if ln.strip())
    n = len(rows)
    ts = sum(1 for o in rows if not o.attributes.get("_no_ts"))
    host = sum(1 for o in rows if o.host)
    service = sum(1 for o in rows if o.service)
    lvl_lines = lvl_hit = 0
    for o in rows:
        m = LEVEL_RE.search(o.raw or o.message)
        if not m:
            continue
        lvl_lines += 1
        if LEVEL_MAP[m.group(1).upper()] == o.severity:
            lvl_hit += 1
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None  # noqa: E731
    shares = {"timestamp": pct(ts, n), "level": pct(lvl_hit, lvl_lines), "host": pct(host, n), "service": pct(service, n)}
    known = [v for v in shares.values() if v is not None]
    return {"file": name, "format": rep.get("format", "-"), "confidence": rep.get("confidence"), "lines": lines, "events": n,
            **shares, "score": round(sum(known) / len(known), 1) if known else 0.0}


def measure_noise(name: str) -> dict:
    """Noise reduction on a sample dataset: raw events -> fingerprints -> meaningful signals -> incidents, the reduction factor,
    the share of raw events eliminated as noise; on the alarm storm also the precision of the noise verdict against the ground truth."""
    from .analysis import Analysis
    from .pipeline import ingest_path
    obs, rep = ingest_path(str(SAMPLES / name))
    a = Analysis(obs, rep)
    f = a.funnel()
    na = a.noise_audit()
    out = {"dataset": name, "raw": f["raw_events"], "fingerprints": f["fingerprints"], "signals": f["meaningful_signals"], "incidents": f["incidents"],
           "reduction": f["reduction"], "eliminated_pct": round(100.0 * na["eliminated"] / max(1, f["raw_events"]), 1), "noise_precision": None}
    truth_file = SAMPLES / name.replace(".zip", "_truth.json")
    if truth_file.exists():
        truth = json.loads(truth_file.read_text(encoding="utf-8"))
        ids = [o.attributes.get("alarm_id") for o in a.noise_obs]
        ids = [i for i in ids if i in truth]
        out["noise_precision"] = round(100.0 * sum(1 for i in ids if truth[i] == "noise") / len(ids), 1) if ids else None
    return out


def run(corpus: Path = CORPUS, noise: bool = True) -> dict:
    files = sorted(p for p in corpus.iterdir() if p.is_file() and p.suffix in (".log", ".jsonl", ".json", ".csv", ".txt") and p.name != BASELINE.name)
    rows = [measure_file(p.name, p.read_text(encoding="utf-8", errors="replace")) for p in files]
    overall = {k: round(sum(r[k] for r in rows if r[k] is not None) / max(1, sum(1 for r in rows if r[k] is not None)), 1) for k in FIELDS}
    overall["score"] = round(sum(r["score"] for r in rows) / max(1, len(rows)), 1)
    ds = [measure_noise(n) for n in DATASETS if (SAMPLES / n).exists()] if noise else []
    return {"files": rows, "overall": overall, "n": len(rows), "noise": ds}


def load_baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def regressions(result: dict, baseline: dict, tolerance: float = 0.5) -> list[str]:
    """Files whose score or any field share dropped below the accepted baseline; datasets whose noise reduction or precision dropped."""
    out = []
    for d in result.get("noise", []):
        b = baseline.get("noise:" + d["dataset"])
        if not b:
            continue
        if d["reduction"] + 0.05 * b["reduction"] < b["reduction"]:                                   # 5 % slack: a few more signals is fine, half is not
            out.append(f"{d['dataset']}: reduction {d['reduction']}x < baseline {b['reduction']}x")
        if b.get("noise_precision") is not None and (d.get("noise_precision") or 0) + tolerance < b["noise_precision"]:
            out.append(f"{d['dataset']}: noise precision {d['noise_precision']} < baseline {b['noise_precision']}")
        if d["incidents"] > 3 * max(1, b["incidents"]):
            out.append(f"{d['dataset']}: incidents {d['incidents']} > 3x baseline {b['incidents']}")
    for r in result["files"]:
        b = baseline.get(r["file"])
        if not b:
            continue
        for k in (*FIELDS, "score"):
            if b.get(k) is not None and r.get(k) is not None and r[k] + tolerance < b[k]:
                out.append(f"{r['file']}: {k} {r[k]} < baseline {b[k]}")
            if b.get(k) is not None and r.get(k) is None:
                out.append(f"{r['file']}: {k} became unmeasurable")
    return out


def table(result: dict) -> str:
    cols = ("file", "format", "lines", "events", "timestamp", "level", "host", "service", "score")
    w = [max(len(c), *(len(str(r.get(c, "-") if r.get(c) is not None else "-")) for r in result["files"])) for c in cols]
    fmt = lambda r: "  ".join(str(r.get(c) if r.get(c) is not None else "-").ljust(w[i]) for i, c in enumerate(cols))  # noqa: E731
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(cols)), "  ".join("-" * x for x in w)]
    lines += [fmt(r) for r in result["files"]]
    o = result["overall"]
    lines.append("")
    lines.append(f"overall ({result['n']} files): timestamp {o['timestamp']}%  level {o['level']}%  host {o['host']}%  service {o['service']}%  score {o['score']}")
    if result.get("noise"):
        lines.append("")
        lines.append("noise reduction: dataset  raw -> fingerprints -> signals -> incidents  reduction  eliminated  noise precision")
        for d in result["noise"]:
            lines.append(f"  {d['dataset']:<18} {d['raw']:>6} -> {d['fingerprints']:>4} -> {d['signals']:>3} -> {d['incidents']:>2}   {d['reduction']}x   {d['eliminated_pct']}%   {d['noise_precision'] if d['noise_precision'] is not None else '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    res = run()
    if "--update-baseline" in argv:
        base = {r["file"]: {k: r[k] for k in (*FIELDS, "score", "events")} for r in res["files"]}
        base.update({"noise:" + d["dataset"]: {k: d[k] for k in ("reduction", "incidents", "signals", "eliminated_pct", "noise_precision")} for d in res.get("noise", [])})
        BASELINE.write_text(json.dumps(base, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"baseline written: {BASELINE} ({res['n']} files, score {res['overall']['score']})")
        return 0
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(table(res))
    reg = regressions(res, load_baseline())
    for r in reg:
        print("REGRESSION:", r)
    return 1 if reg else 0


if __name__ == "__main__":
    raise SystemExit(main())
