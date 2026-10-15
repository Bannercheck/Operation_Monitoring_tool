#!/usr/bin/env python3
"""Golden outputs of the Python parsers, for the Go parsers' parity tests (receiver/parse).

Inputs: every file in samples/corpus, plus receiver/parse/testdata/inputs (built once with --build-inputs: the SAP samples,
the demo dataset, a slice of the alarm storm, simulator output and seeded variations of every corpus file, so the parity tests
cover thousands of lines rather than a few per format). For every input two runs are recorded:
  <file>.whole.jsonl.gz  the whole file through pipeline.ingest_bytes (no hint), as an upload is parsed
  <file>.live.jsonl.gz   the file cut into 40-line batches and parsed batch by batch with the per-stream hint, as live.py does
Each line is one observation in canonical JSON (sorted keys); the first line of each file is the parse report
({"report": …}). Timestamps without a year take the current year, so regenerate after New Year.

  PYTHONPATH=src python scripts/parse_golden.py [--out receiver/parse/testdata/golden]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def canon(o) -> dict:
    return {"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity, "service": o.service, "host": o.host,
            "environment": o.environment, "origin": o.origin, "attributes": o.attributes, "source": o.source, "line_no": o.line_no,
            "parser": o.parser, "parser_confidence": round(float(o.parser_confidence), 4), "raw": o.raw}


def dump(path: Path, report, obs) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(json.dumps({"report": report}, sort_keys=True, ensure_ascii=False, default=str) + "\n")
        for o in obs:
            f.write(json.dumps(canon(o), sort_keys=True, ensure_ascii=False, default=str) + "\n")


def build_inputs(dst: Path) -> None:
    """Stored once, so the goldens and the Go tests read the very same bytes (the simulator and the variations are random)."""
    import random
    import zipfile
    from datetime import datetime, timezone
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted((ROOT / "samples" / "sap").iterdir()):
        if p.is_file():
            (dst / f"sap__{p.name}").write_bytes(p.read_bytes())
    with zipfile.ZipFile(ROOT / "samples" / "demo_mixed.zip") as z:
        for n in z.namelist():
            if not n.endswith("/"):
                (dst / f"demo__{Path(n).name}").write_bytes(z.read(n))
    with zipfile.ZipFile(ROOT / "samples" / "alarm_storm.zip") as z:
        for n in ("alarms.csv", "alarms.json"):
            data = z.read(n)
            if n.endswith(".csv"):
                data = b"\n".join(data.split(b"\n")[:1500])
            (dst / f"storm__{n}").write_bytes(data)
    sys.path.insert(0, str(ROOT / "scripts"))
    import loadtest
    from watchover import live as wl
    rng = random.Random(7)
    now = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    sim = b"".join(wl.simulate_batch(rng, 30, incident=i % 5 == 0) for i in range(40))
    (dst / "sim__simulate_batch.jsonl").write_bytes(sim)
    tr = [ln for i in range(60) for ln in wl.simulate_traces(rng, now, 3, incident=i % 7 == 0)]
    (dst / "sim__simulate_traces.jsonl").write_text("\n".join(tr) + "\n", encoding="utf-8")
    (dst / "sim__simulate_flows.jsonl").write_bytes(wl.simulate_flows(rng))
    for p in sorted((ROOT / "samples" / "corpus").iterdir()):
        if p.suffix == ".md" or p.is_dir():
            continue
        lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if not lines or p.suffix == ".json":                 # whole JSON documents do not split into lines
            continue
        r = random.Random(p.name)
        head = lines[:1] if p.suffix in (".csv",) or p.name.startswith("iis_") else []
        body = [loadtest.freshen(r.choice(lines[len(head):] or lines), now, r) for _ in range(240)]
        (dst / f"mut__{p.name}").write_text("\n".join(head + body) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "receiver" / "parse" / "testdata" / "golden"))
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--build-inputs", action="store_true", help="(re)write receiver/parse/testdata/inputs first")
    a = ap.parse_args(argv)
    inputs = ROOT / "receiver" / "parse" / "testdata" / "inputs"
    if a.build_inputs or not inputs.exists():
        build_inputs(inputs)
    os.environ.setdefault("WATCHOVER_HOME", tempfile.mkdtemp())          # no learned profiles: the parsers' own decisions only
    from watchover.pipeline import ingest_bytes
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted((ROOT / "samples" / "corpus").iterdir()) if p.suffix != ".md" and p.is_file()] + sorted(inputs.iterdir())
    for p in files:
        data = p.read_bytes()
        obs, rep = ingest_bytes(p.name, data)
        dump(out / f"{p.name}.whole.jsonl.gz", [{k: v for k, v in r.items() if k not in ("sha", "table")} for r in rep], obs)
        lines = data.split(b"\n")
        live, hint, reps = [], None, []
        for i in range(0, len(lines), a.batch):
            chunk = b"\n".join(lines[i:i + a.batch])
            if not chunk.strip():
                continue
            if hint is not None:
                hint["n"] += 1
                if hint["n"] % 200 == 0:
                    hint = None
            o, r = ingest_bytes(p.name, chunk, hint=hint)
            if r and r[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
                fmt = r[0]["format"]
                if fmt == "json" and chunk.strip().count(b"\n") == 0 and chunk.lstrip()[:1] == b"{":
                    fmt = "jsonl"
                hint = {"format": fmt, "confidence": r[0].get("confidence", 0.9), "family": r[0].get("family", ""), "n": 1}
            live.extend(o); reps.append({k: v for k, v in (r[0] if r else {}).items() if k not in ("sha", "table")})
        dump(out / f"{p.name}.live.jsonl.gz", reps[:3], live)
        print(f"{p.name:34s} whole {len(obs):5d}  live {len(live):5d}  {rep[0].get('format') if rep else '-'} {rep[0].get('family', '') if rep else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
