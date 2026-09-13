#!/usr/bin/env python3
"""Inspect an unknown dataset and report what it contains.

Usage:
    python tools/inspect_dataset.py <file|zip|gz|directory> [--lines N] [--json]

Standard library only. For every text file it reports: detected format,
confidence, row count, columns/keys with fill rate and example values,
likely timestamp / severity / service / host / message fields, time range,
and sample lines. Use it on the hackathon dataset before touching any code.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

SAMPLE = 200  # lines used for sniffing
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".parquet", ".xlsx", ".xls", ".db", ".sqlite", ".pkl"}

ROLE_HINTS = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created", "logged_at"),
    "severity": ("severity", "level", "loglevel", "log_level", "priority", "status", "sev"),
    "service": ("service", "app", "application", "component", "logger", "source", "module", "job", "program", "svc"),
    "host": ("host", "hostname", "node", "instance", "server", "pod", "container"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title", "body", "line"),
}

TS_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"),
    re.compile(r"\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}"),
    re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}"),  # syslog
    re.compile(r"^\d{10}(\.\d+)?$"),  # epoch seconds
    re.compile(r"^\d{13}$"),  # epoch millis
]
SYSLOG_RE = re.compile(r"^(<\d+>)?[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\s+\S+\s+\S+")
KV_RE = re.compile(r"(\w+)=(\"[^\"]*\"|\S+)")
LEVEL_WORDS = {"TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR", "ERR", "CRITICAL", "CRIT", "FATAL", "ALERT", "EMERG"}


# ---------- loading ----------

def iter_files(path: Path):
    """Yield (display_name, text) for every text file under path (zip/gz aware)."""
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield from iter_files(p)
        return
    yield from iter_bytes(str(path), path.read_bytes())


def iter_bytes(name: str, data: bytes):
    lower = name.lower()
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir():
                        yield from iter_bytes(f"{name}!{info.filename}", zf.read(info))
        except zipfile.BadZipFile:
            yield name, None
        return
    if lower.endswith(".gz"):
        yield from iter_bytes(name[:-3], gzip.decompress(data))
        return
    if Path(lower).suffix in BINARY_EXT or b"\x00" in data[:4096]:
        yield name, None
        return
    yield name, data.decode("utf-8-sig", errors="replace")


# ---------- sniffing ----------

def sniff(lines: list[str]) -> tuple[str, float, dict]:
    """Return (format, confidence, extra) for sample lines."""
    if not lines:
        return "empty", 1.0, {}
    joined = "\n".join(lines)

    # whole-file JSON (array or object)
    if lines[0].lstrip().startswith(("[", "{")):
        try:
            obj = json.loads(joined)
            if isinstance(obj, list):
                return "json_array", 0.95, {"records": obj}
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return "json_object", 0.9, {"records": v, "records_key": k}
                return "json_object", 0.7, {"records": [obj]}
        except json.JSONDecodeError:
            pass

    # JSONL
    ok = 0
    for ln in lines:
        try:
            if isinstance(json.loads(ln), dict):
                ok += 1
        except json.JSONDecodeError:
            pass
    if ok / len(lines) > 0.8:
        return "jsonl", ok / len(lines), {}

    # syslog
    hits = sum(1 for ln in lines if SYSLOG_RE.match(ln))
    if hits / len(lines) > 0.6:
        return "syslog", hits / len(lines), {}

    # CSV / TSV via Sniffer, then consistency check
    for delim in ("\t", ",", ";", "|"):
        counts = [ln.count(delim) for ln in lines[:50]]
        if counts and counts[0] >= 1 and len(set(counts)) <= 2 and min(counts) >= 1:
            fmt = "tsv" if delim == "\t" else "csv"
            return fmt, 0.85, {"delimiter": delim}

    # key=value
    kv = sum(1 for ln in lines if len(KV_RE.findall(ln)) >= 2)
    if kv / len(lines) > 0.6:
        return "kv", kv / len(lines), {}

    return "plain", 0.5, {}


# ---------- record extraction ----------

def records_from(fmt: str, text: str, extra: dict) -> list[dict]:
    lines = text.splitlines()
    if fmt in ("json_array", "json_object"):
        recs = extra.get("records")
        if recs is None:
            obj = json.loads(text)
            recs = obj if isinstance(obj, list) else obj.get(extra.get("records_key"), [obj])
        return [flatten(r) for r in recs if isinstance(r, dict)]
    if fmt == "jsonl":
        out = []
        for ln in lines:
            try:
                r = json.loads(ln)
                if isinstance(r, dict):
                    out.append(flatten(r))
            except json.JSONDecodeError:
                pass
        return out
    if fmt in ("csv", "tsv"):
        reader = csv.DictReader(io.StringIO(text), delimiter=extra.get("delimiter", ","))
        return [dict(r) for r in reader]
    if fmt == "kv":
        return [{k: v.strip('"') for k, v in KV_RE.findall(ln)} for ln in lines if ln.strip()]
    if fmt == "syslog":
        out = []
        for ln in lines:
            m = re.match(r"^(?:<\d+>)?([A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+(\S+)\s+([^:\[]+)(?:\[(\d+)\])?:\s*(.*)$", ln)
            if m:
                out.append({"timestamp": m[1], "host": m[2], "program": m[3], "pid": m[4], "message": m[5]})
        return out
    return [{"line": ln} for ln in lines if ln.strip()]


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


# ---------- profiling ----------

def looks_like_ts(v) -> bool:
    s = str(v)
    return any(p.search(s) for p in TS_PATTERNS)


def parse_ts(v):
    s = str(v).strip()
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s))
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S,%f", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.match(r"^([A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})", s)
    if m:
        try:
            return datetime.strptime(f"{datetime.now().year} {m[1]}", "%Y %b %d %H:%M:%S")
        except ValueError:
            return None
    return None


def profile_columns(records: list[dict]) -> list[dict]:
    if not records:
        return []
    n = len(records)
    cols: dict[str, Counter] = {}
    filled: Counter = Counter()
    for r in records:
        for k, v in r.items():
            if v is None or v == "":
                continue
            filled[k] += 1
            cols.setdefault(k, Counter())[str(v)[:80]] += 1
    out = []
    for k, ctr in cols.items():
        vals = list(ctr.keys())
        sample = vals[:3]
        ts_share = sum(ctr[v] for v in vals if looks_like_ts(v)) / filled[k]
        num_share = sum(ctr[v] for v in vals if re.fullmatch(r"-?\d+(\.\d+)?", v)) / filled[k]
        level_share = sum(ctr[v] for v in vals if v.upper() in LEVEL_WORDS) / filled[k]
        out.append({
            "column": k,
            "fill_rate": round(filled[k] / n, 3),
            "distinct": len(ctr),
            "timestamp_like": round(ts_share, 2),
            "numeric": round(num_share, 2),
            "level_like": round(level_share, 2),
            "top_values": [f"{v} ({c})" for v, c in ctr.most_common(3)],
            "examples": sample,
        })
    return out


def guess_roles(profile: list[dict]) -> dict[str, str | None]:
    by_name = {p["column"].lower(): p["column"] for p in profile}
    roles: dict[str, str | None] = {}
    for role, names in ROLE_HINTS.items():
        roles[role] = next((by_name[n] for n in names if n in by_name), None)
        if roles[role] is None:
            for n in names:  # suffix match, e.g. event.timestamp
                hit = next((c for lc, c in by_name.items() if lc.endswith("." + n) or lc.endswith("_" + n)), None)
                if hit:
                    roles[role] = hit
                    break
    if roles["timestamp"] is None:
        best = max(profile, key=lambda p: p["timestamp_like"], default=None)
        if best and best["timestamp_like"] > 0.8:
            roles["timestamp"] = best["column"]
    if roles["severity"] is None:
        best = max(profile, key=lambda p: p["level_like"], default=None)
        if best and best["level_like"] > 0.8:
            roles["severity"] = best["column"]
    if roles["message"] is None:
        # longest average text column
        taken = {v for v in roles.values() if v}
        cands = [p for p in profile if p["column"] not in taken and p["timestamp_like"] < 0.5 and p["numeric"] < 0.5]
        best = max(cands, key=lambda p: sum(len(x) for x in p["examples"]) / max(len(p["examples"]), 1), default=None)
        if best:
            roles["message"] = best["column"]
    return roles


def time_range(records: list[dict], ts_col: str | None):
    if not ts_col:
        return None
    stamps = [t for t in (parse_ts(r.get(ts_col)) for r in records if r.get(ts_col)) if t]
    if not stamps:
        return {"parsed": 0, "note": "timestamp column found but no known format parsed"}
    stamps.sort()
    span = stamps[-1] - stamps[0]
    return {"parsed": len(stamps), "start": stamps[0].isoformat(), "end": stamps[-1].isoformat(),
            "span": str(span), "avg_per_minute": round(len(stamps) / max(span.total_seconds() / 60, 1), 2)}


def inspect_file(name: str, text: str | None, sample_lines: int) -> dict:
    if text is None:
        return {"file": name, "format": "binary/unsupported", "note": "skipped"}
    lines = text.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    fmt, conf, extra = sniff(nonempty[:SAMPLE])
    records = records_from(fmt, text, extra)
    profile = profile_columns(records)
    roles = guess_roles(profile)
    sev_col = roles.get("severity")
    sev_dist = Counter(str(r.get(sev_col)) for r in records if r.get(sev_col)) if sev_col else Counter()
    return {
        "file": name,
        "format": fmt,
        "confidence": round(conf, 2),
        "lines": len(lines),
        "records": len(records),
        "bytes": len(text.encode("utf-8", errors="replace")),
        "roles": roles,
        "time_range": time_range(records, roles.get("timestamp")),
        "severity_distribution": dict(sev_dist.most_common(10)),
        "columns": profile,
        "sample_lines": nonempty[:sample_lines],
    }


# ---------- output ----------

def print_report(reports: list[dict]) -> None:
    for r in reports:
        print("=" * 78)
        print(f"FILE     {r['file']}")
        print(f"FORMAT   {r['format']}  (confidence {r.get('confidence', '-')})")
        if r["format"] == "binary/unsupported":
            continue
        print(f"SIZE     {r['lines']} lines, {r['records']} records, {r['bytes']:,} bytes")
        print("ROLES    " + ", ".join(f"{k}={v or '?'}" for k, v in r["roles"].items()))
        tr = r["time_range"]
        if tr:
            if "start" in tr:
                print(f"TIME     {tr['start']} -> {tr['end']}  span {tr['span']}  ~{tr['avg_per_minute']}/min  ({tr['parsed']} parsed)")
            else:
                print(f"TIME     {tr['note']}")
        if r["severity_distribution"]:
            print("SEVERITY " + ", ".join(f"{k}:{v}" for k, v in r["severity_distribution"].items()))
        print("COLUMNS")
        for c in sorted(r["columns"], key=lambda c: -c["fill_rate"])[:40]:
            flags = []
            if c["timestamp_like"] > 0.8: flags.append("ts")
            if c["numeric"] > 0.8: flags.append("num")
            if c["level_like"] > 0.8: flags.append("level")
            print(f"  {c['column']:<32} fill {c['fill_rate']:>5}  distinct {c['distinct']:>6}  {'/'.join(flags):<9} {', '.join(c['top_values'])[:70]}")
        if len(r["columns"]) > 40:
            print(f"  ... {len(r['columns']) - 40} more columns")
        print("SAMPLE")
        for ln in r["sample_lines"]:
            print("  " + ln[:160])
    print("=" * 78)
    fmts = Counter(r["format"] for r in reports)
    print("SUMMARY  " + ", ".join(f"{k}: {v} file(s)" for k, v in fmts.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--lines", type=int, default=5, help="sample lines to show per file")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 1
    reports = [inspect_file(n, t, args.lines) for n, t in iter_files(path)]
    if args.json:
        json.dump(reports, sys.stdout, indent=2, default=str)
    else:
        print_report(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
