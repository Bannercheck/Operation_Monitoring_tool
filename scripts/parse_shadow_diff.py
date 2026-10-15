#!/usr/bin/env python3
"""Shadow check of the Go parsers (WATCHOVER_GO_PARSE=shadow) on real traffic.

In shadow mode the receiver keeps handing raw segments to the dashboard and also writes shadow-<ns>.wo into the spool: every
/ingest record as received (v=1), followed by the record Go produced for it (v=2, "shadow": true) when Go parsed the batch.
This script replays the raw records through the dashboard's own parse step (LiveStore._parse: the per-stream hint, then
pipeline.ingest_bytes), renders each result the way a v=2 body is written and compares it with Go's, per format[:family].

  PYTHONPATH=src python scripts/parse_shadow_diff.py [--spool DIR] [--out shadow-diff.json] [--delete] [--follow SECONDS]

A format graduates to WATCHOVER_GO_PARSE=1 after 0 mismatches over >= 24 h of traffic (docs/go-parse/06-integration.md §10).
Learned profiles: the dashboard's profiles.json is copied into a scratch WATCHOVER_HOME, so the replay never writes the real one.
Exit status: 0 when nothing differed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def spool_line(o) -> str:
    """v=2 body line: golden canon() (scripts/parse_golden.py), sorted top-level keys, attributes in insertion order."""
    d = {"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity, "service": o.service, "host": o.host,
         "environment": o.environment, "origin": o.origin, "attributes": o.attributes, "source": o.source, "line_no": o.line_no,
         "parser": o.parser, "parser_confidence": round(float(o.parser_confidence), 4), "raw": o.raw}
    return json.dumps({k: d[k] for k in sorted(d)}, ensure_ascii=False, default=str)


def records(path: Path):
    data = path.read_bytes()
    i = 0
    while i < len(data):
        nl = data.find(b"\n", i)
        if nl < 0:
            return
        try:
            h = json.loads(data[i:nl])
            n = int(h["len"])
        except (ValueError, KeyError, TypeError):
            return
        body = data[nl + 1:nl + 1 + n]
        if len(body) < n:
            return
        yield h, body
        i = nl + 1 + n + 1


class Diff:
    def __init__(self):
        from watchover.live import LiveStore
        self.store = LiveStore(spool=None)              # only its parse step is used: same hint cache as the dashboard
        self.by_fmt: dict[str, dict] = defaultdict(lambda: {"compared": 0, "mismatched": 0, "go_only_raw": 0})
        self.samples: list[dict] = []
        self.raw_batches = 0

    def check(self, raw_h: dict, raw_body: bytes, go: tuple[dict, bytes] | None) -> None:
        agent, name = raw_h.get("agent") or "unknown", raw_h.get("file") or "agent.log"
        self.raw_batches += 1
        try:
            obs = self.store._parse(name, raw_body, agent)
            err = None
        except Exception as e:  # noqa: BLE001 - Python failing where Go parsed is a mismatch too
            obs, err = [], f"{type(e).__name__}: {e}"
        if go is None:
            return
        gh, gbody = go
        key = gh.get("format", "") + (":" + gh.get("family", "") if gh.get("format") in ("text", "grok") or gh.get("family") else "")
        st = self.by_fmt[key]
        st["compared"] += 1
        want = [spool_line(o) for o in obs]
        got = gbody.decode("utf-8", "replace").split("\n")[:-1] if gbody else []
        hint = self.store._detect.get((agent, name)) or {}
        problems = []
        if err:
            problems.append(f"python raised {err}")
        if want != got:
            idx = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
            problems.append({"line": idx, "python": want[idx] if idx < len(want) else None, "go": got[idx] if idx < len(got) else None,
                             "python_n": len(want), "go_n": len(got)})
        if hint and hint.get("n") == 1 and hint.get("format") != gh.get("hint_format"):
            problems.append({"hint_format": {"python": hint.get("format"), "go": gh.get("hint_format")}})
        if problems:
            st["mismatched"] += 1
            if len(self.samples) < 100:
                self.samples.append({"format": key, "agent": agent, "file": name, "ts": raw_h.get("ts"), "problems": problems})

    def segment(self, path: Path) -> None:
        pending = None
        for h, body in records(path):
            if h.get("v") == 2 and h.get("shadow"):
                if pending is not None:
                    self.check(*pending, (h, body))
                    pending = None
                continue
            if pending is not None:
                self.check(*pending, None)
            pending = (h, body) if (h.get("path") or "/ingest") == "/ingest" else None
        if pending is not None:
            self.check(*pending, None)

    def report(self) -> dict:
        tot = sum(v["mismatched"] for v in self.by_fmt.values())
        return {"raw_batches": self.raw_batches, "compared": sum(v["compared"] for v in self.by_fmt.values()), "mismatched": tot,
                "by_format": dict(sorted(self.by_fmt.items())), "samples": self.samples}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    home = os.environ.get("WATCHOVER_HOME") or "data"
    ap.add_argument("--spool", default=os.environ.get("WATCHOVER_SPOOL") or os.path.join(home, "spool"))
    ap.add_argument("--out", default="shadow-diff.json")
    ap.add_argument("--delete", action="store_true", help="remove shadow segments once compared")
    ap.add_argument("--follow", type=float, default=0, help="keep watching the spool, checking every N seconds")
    a = ap.parse_args(argv)
    scratch = Path(tempfile.mkdtemp(prefix="wo-shadow-"))
    for src in (Path(home) / "profiles.json", Path(a.spool) / "profiles.json"):
        if src.exists():
            d = json.loads(src.read_text(encoding="utf-8"))
            d.pop("_scenario_mapping", None)
            (scratch / "profiles.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            break
    os.environ["WATCHOVER_HOME"] = str(scratch)
    sys.path.insert(0, str(ROOT / "src"))
    diff, done = Diff(), set()
    try:
        while True:
            segs = [p for p in sorted(Path(a.spool).glob("shadow-*.wo")) if p.name not in done]
            for p in segs:
                diff.segment(p)
                done.add(p.name)
                if a.delete:
                    p.unlink(missing_ok=True)
            rep = diff.report()
            Path(a.out).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
            if not a.follow:
                break
            time.sleep(a.follow)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps({k: v for k, v in rep.items() if k != "samples"}, indent=1))
    return 0 if rep["mismatched"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
