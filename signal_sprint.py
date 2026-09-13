#!/usr/bin/env python3
"""Signal Sprint: operational noise -> signals -> explained incidents -> tracked actions.

Single file, standard library only (Python 3.10+). No runtime LLM or API.

Usage:
    python signal_sprint.py inspect <file|zip|dir>              # what is in the dataset?
    python signal_sprint.py analyze <file|zip|dir> [--json|--md] # run the pipeline, print results
    python signal_sprint.py serve [<file|zip|dir>] [--port 8000]  # web dashboard (upload also supported)

Pipeline:
    load -> detect format -> parse to Observation -> fingerprint (template mining)
    -> Signal (dedup + burst detection) -> correlate (time window + shared entities)
    -> Incident (root cause + scored rationale + evidence) -> actions (SQLite)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse, parse_qs

# =============================================================================
# 1. CANONICAL MODEL
# =============================================================================

SEV_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}


@dataclass
class Observation:
    """One dataset row, whatever its original format."""
    timestamp: datetime
    message: str
    kind: str = "log"            # log | event | alert | metric
    severity: str = "INFO"
    service: str = ""
    host: str = ""
    attributes: dict = field(default_factory=dict)
    source: str = ""
    line_no: int = 0
    parser: str = ""
    template: str = ""           # filled by fingerprinting
    fingerprint: str = ""

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.line_no}"


@dataclass
class Signal:
    """Observations sharing one fingerprint, with burst statistics."""
    id: str
    fingerprint: str
    template: str
    severity: str
    count: int
    services: list[str]
    hosts: list[str]
    entities: set[str]
    first_seen: datetime
    last_seen: datetime
    onset: datetime
    peak_rate: float = 0.0
    baseline_rate: float = 0.0
    burst_score: float = 0.0
    observations: list[Observation] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        return [o.ref for o in self.observations[:10]]


@dataclass
class Factor:
    name: str
    weight: float
    value: str
    contribution: float


@dataclass
class Incident:
    id: str
    title: str
    severity: str
    score: float
    root_cause_signal: str
    root_cause_reason: str
    affected_services: list[str]
    affected_hosts: list[str]
    started_at: datetime
    ended_at: datetime
    signal_ids: list[str]
    factors: list[Factor]
    evidence: list[str]
    timeline: list[dict]
    narrative: str


# =============================================================================
# 2. LOADING + FORMAT DETECTION
# =============================================================================

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".parquet", ".xlsx", ".xls", ".db", ".sqlite", ".pkl"}


def iter_files(name: str, data: bytes) -> Iterator[tuple[str, str]]:
    """Yield (file_name, text) for every text file inside a file / zip / gz."""
    lower = name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir() and not info.filename.startswith("__MACOSX"):
                    yield from iter_files(info.filename, zf.read(info))
        return
    if lower.endswith(".gz"):
        yield from iter_files(name[:-3], gzip.decompress(data))
        return
    if Path(lower).suffix in BINARY_EXT or b"\x00" in data[:4096]:
        return
    yield name, data.decode("utf-8-sig", errors="replace")


def iter_path(path: Path) -> Iterator[tuple[str, str]]:
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield from iter_files(str(p.relative_to(path)), p.read_bytes())
    else:
        yield from iter_files(path.name, path.read_bytes())


SYSLOG_RE = re.compile(
    r"^(?:<(?P<pri>\d+)>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+"
    r"(?P<host>\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")
KV_RE = re.compile(r'([\w.\-@]+)=("(?:[^"\\]|\\.)*"|\S+)')
DELIMS = ("\t", ",", ";", "|")


def detect_delimiter(lines: list[str]) -> str | None:
    for d in DELIMS:
        counts = [ln.count(d) for ln in lines[:30]]
        if counts and min(counts) >= 1 and len(set(counts)) <= 2:
            return d
    return None


def detect_format(text: str) -> tuple[str, float]:
    """Return (format, confidence). Formats: json, jsonl, csv, syslog, kv, plain."""
    lines = [ln for ln in text.splitlines()[:200] if ln.strip()]
    if not lines:
        return "plain", 0.0
    first = lines[0].lstrip()
    if first.startswith("[") or (first.startswith("{") and not first.rstrip().endswith("}")):
        try:
            json.loads(text)
            return "json", 0.95
        except json.JSONDecodeError:
            pass
    ok = 0
    for ln in lines:
        try:
            ok += isinstance(json.loads(ln), dict)
        except json.JSONDecodeError:
            pass
    if ok / len(lines) > 0.8:
        return "jsonl", ok / len(lines)
    hits = sum(1 for ln in lines if SYSLOG_RE.match(ln))
    if hits / len(lines) > 0.6:
        return "syslog", hits / len(lines)
    d = detect_delimiter(lines)
    if d and len(lines) >= 2:
        header = lines[0].split(d)
        alpha = sum(1 for h in header if h.strip().replace("_", "").replace(" ", "").isalpha())
        if alpha / len(header) > 0.6:
            return "csv", 0.9
    kv = sum(1 for ln in lines if len(KV_RE.findall(ln)) >= 2)
    if kv / len(lines) > 0.6:
        return "kv", kv / len(lines)
    return "plain", 0.5


# =============================================================================
# 3. NORMALISATION HELPERS
# =============================================================================

SEVERITY_MAP = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "informational": "INFO", "notice": "INFO", "ok": "INFO",
    "warn": "WARN", "warning": "WARN", "error": "ERROR", "err": "ERROR", "critical": "CRITICAL", "crit": "CRITICAL",
    "fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL", "severe": "CRITICAL",
    "high": "ERROR", "medium": "WARN", "low": "INFO", "p1": "CRITICAL", "p2": "ERROR", "p3": "WARN", "p4": "INFO",
    "firing": "ERROR", "resolved": "INFO",
}
ROLE_HINTS = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created_at", "logged_at", "t"),
    "severity": ("severity", "level", "loglevel", "log_level", "priority", "sev", "status"),
    "service": ("service", "app", "application", "component", "logger", "source", "module", "job", "program", "svc", "service_name"),
    "host": ("host", "hostname", "node", "instance", "server", "pod", "container", "machine"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title", "body", "line", "event", "alert", "alertname"),
}
TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%d/%b/%Y:%H:%M:%S %z",
)
SYSLOG_TS_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s(\d{2}):(\d{2}):(\d{2})")
LEVEL_WORD_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|ALERT|EMERG)\b", re.I)
UTC = timezone.utc


def parse_timestamp(value: Any) -> datetime | None:
    """Parse many timestamp shapes; always tz-aware (naive input assumed UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=UTC)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{13}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=UTC)
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=UTC)
    s2 = s[:-1] + "+0000" if s.endswith("Z") else s
    dt = None
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(s2, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        m = SYSLOG_TS_RE.match(s)
        if m:
            try:
                dt = datetime.strptime(f"{datetime.now().year} {m[1]} {m[2]} {m[3]}:{m[4]}:{m[5]}", "%Y %b %d %H:%M:%S")
            except ValueError:
                return None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def normalize_severity(value: Any) -> str:
    if value is None:
        return "INFO"
    s = str(value).strip().lower()
    if s in SEVERITY_MAP:
        return SEVERITY_MAP[s]
    if s.isdigit():
        return {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR", 4: "WARN"}.get(int(s), "INFO")
    up = s.upper()
    return up if up in SEV_RANK else "INFO"


def resolve_roles(keys: list[str], mapping: dict | None = None) -> dict[str, str | None]:
    """Which key plays which role. Explicit mapping wins over name heuristics."""
    lowered = {k.lower(): k for k in keys}
    out: dict[str, str | None] = {}
    for role, hints in ROLE_HINTS.items():
        if mapping and mapping.get(role) in keys:
            out[role] = mapping[role]
            continue
        hit = next((lowered[h] for h in hints if h in lowered), None)
        if hit is None:
            hit = next((k for lk, k in lowered.items()
                        if any(lk.endswith(sep + h) for h in hints for sep in ("_", ".", "-"))), None)
        out[role] = hit
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


# =============================================================================
# 4. PARSERS (each yields (line_no, record dict); records become Observations)
# =============================================================================

def records_json(text: str) -> Iterator[tuple[int, dict]]:
    obj = json.loads(text)
    if isinstance(obj, dict):
        obj = next((v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [obj])
    for i, r in enumerate(obj, 1):
        if isinstance(r, dict):
            yield i, flatten(r)


def records_jsonl(text: str) -> Iterator[tuple[int, dict]]:
    for i, ln in enumerate(text.splitlines(), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            yield i, flatten(r)


def records_csv(text: str) -> Iterator[tuple[int, dict]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    d = detect_delimiter(lines) or ","
    for i, row in enumerate(csv.DictReader(io.StringIO(text), delimiter=d), 2):
        yield i, {k.strip(): v for k, v in row.items() if k is not None}


def records_syslog(text: str) -> Iterator[tuple[int, dict]]:
    for i, ln in enumerate(text.splitlines(), 1):
        m = SYSLOG_RE.match(ln)
        if not m:
            continue
        rec = {"timestamp": m["ts"], "host": m["host"], "program": m["prog"], "message": m["msg"]}
        if m["pid"]:
            rec["pid"] = m["pid"]
        if m["pri"]:
            rec["severity"] = str(int(m["pri"]) % 8)
        yield i, rec


def records_kv(text: str) -> Iterator[tuple[int, dict]]:
    for i, ln in enumerate(text.splitlines(), 1):
        pairs = KV_RE.findall(ln)
        if len(pairs) >= 2:
            yield i, {k: v[1:-1].replace('\\"', '"') if v.startswith('"') else v for k, v in pairs}


PLAIN_TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+\-]\d{2}:?\d{2})?)\]?\s*")
PLAIN_LEVEL_RE = re.compile(r"^\[?(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL)\]?[:\s]+", re.I)
PLAIN_LOGGER_RE = re.compile(r"^\[?([A-Za-z][\w.\-]{2,})\]?:\s+")


def records_plain(text: str) -> Iterator[tuple[int, dict]]:
    for i, ln in enumerate(text.splitlines(), 1):
        if not ln.strip():
            continue
        rec: dict = {}
        rest = ln
        m = PLAIN_TS_RE.match(rest)
        if m:
            rec["timestamp"], rest = m[1], rest[m.end():]
        m = PLAIN_LEVEL_RE.match(rest)
        if m:
            rec["severity"], rest = m[1], rest[m.end():]
        m = PLAIN_LOGGER_RE.match(rest)
        if m:
            rec["service"], rest = m[1], rest[m.end():]
        rec["message"] = rest.strip()
        yield i, rec


PARSERS = {"json": records_json, "jsonl": records_jsonl, "csv": records_csv,
           "syslog": records_syslog, "kv": records_kv, "plain": records_plain}
KIND_BY_FORMAT = {"json": "event", "jsonl": "event", "csv": "event", "syslog": "log", "kv": "log", "plain": "log"}


def to_observations(records: Iterator[tuple[int, dict]], source: str, fmt: str,
                    mapping: dict | None = None) -> Iterator[Observation]:
    records = iter(records)
    head = []
    for _ in range(50):
        try:
            head.append(next(records))
        except StopIteration:
            break
    keys: list[str] = []
    for _, rec in head:
        keys.extend(k for k in rec if k not in keys)
    roles = resolve_roles(keys, mapping)
    used = {v for v in roles.values() if v}
    last_ts = datetime.fromtimestamp(0, tz=UTC)
    kind = KIND_BY_FORMAT.get(fmt, "log")
    if "alert" in source.lower() or roles["message"] in ("alert", "alertname"):
        kind = "alert"
    import itertools
    for line_no, rec in itertools.chain(head, records):
        ts = parse_timestamp(rec.get(roles["timestamp"])) if roles["timestamp"] else None
        ts = ts or last_ts
        last_ts = ts
        msg = str(rec.get(roles["message"], "") or "") if roles["message"] else " ".join(f"{k}={v}" for k, v in rec.items())
        sev_raw = rec.get(roles["severity"]) if roles["severity"] else None
        if sev_raw not in (None, ""):
            sev = normalize_severity(sev_raw)
        else:
            m = LEVEL_WORD_RE.search(msg)
            sev = normalize_severity(m[1]) if m else "INFO"
        yield Observation(
            timestamp=ts, message=msg, kind=kind, severity=sev,
            service=str(rec.get(roles["service"]) or "") if roles["service"] else "",
            host=str(rec.get(roles["host"]) or "") if roles["host"] else "",
            attributes={k: v for k, v in rec.items() if k not in used and v not in (None, "")},
            source=source, line_no=line_no, parser=fmt)


def ingest(files: Iterator[tuple[str, str]], mapping: dict | None = None) -> tuple[list[Observation], list[dict]]:
    """All files -> sorted Observations + per-file report."""
    observations: list[Observation] = []
    report: list[dict] = []
    for fname, text in files:
        fmt, conf = detect_format(text)
        rows = list(to_observations(PARSERS[fmt](text), fname, fmt, mapping))
        observations.extend(rows)
        report.append({"file": fname, "format": fmt, "confidence": round(conf, 2), "rows": len(rows)})
    observations.sort(key=lambda o: o.timestamp)
    return observations, report


# =============================================================================
# 5. NOISE REDUCTION: template mining, fingerprint, signals, burst detection
# =============================================================================

MASKS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*"), "<ts>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"(?<=[\s=:/])/[\w./\-]+"), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(ms|s|sec|m|h|%|kb|mb|gb)\b", re.I), "<n>\\1"),
    (re.compile(r"\b\d+\b"), "<n>"),
]
ENTITY_RE = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z][\w\-]*(?:-\d+|\.[a-z]+)+|[a-z]+-(?:api|db|svc|service|cache|queue|worker|gateway|proxy)\b)", re.I)
DEPENDENCY_WORDS = ("database", "db", "postgres", "mysql", "redis", "kafka", "queue", "connection", "network", "dns",
                    "disk", "storage", "latency", "duration", "slow", "certificate", "auth", "gateway", "no space", "oom")


def template_of(message: str) -> str:
    t = message.strip()
    for rx, rep in MASKS:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t).lower()[:200]


def entities_of(o: Observation) -> set[str]:
    ents = {x.lower() for x in ENTITY_RE.findall(o.message)}
    ents.update(x for x in (o.service.lower(), o.host.lower()) if x)
    for k, v in o.attributes.items():
        if isinstance(v, str) and re.fullmatch(r"[\w.\-]{3,40}", v) and any(h in k.lower() for h in ("id", "host", "service", "node", "ip", "target", "endpoint", "db", "pod")):
            ents.add(v.lower())
    return ents


def fingerprint(observations: list[Observation]) -> None:
    for o in observations:
        o.template = template_of(o.message)
        o.fingerprint = hashlib.sha1(f"{o.template}|{o.severity}|{o.service.lower()}".encode()).hexdigest()[:12]


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
        for o in obs:
            ents |= entities_of(o)
        sig = Signal(id=f"S{n}", fingerprint=fp, template=obs[0].template, severity=obs[0].severity, count=len(obs),
                     services=sorted({o.service for o in obs if o.service}), hosts=sorted({o.host for o in obs if o.host}),
                     entities=ents, first_seen=obs[0].timestamp, last_seen=obs[-1].timestamp, onset=obs[0].timestamp,
                     observations=obs)
        score_burst(sig, span_min)
        signals.append(sig)
    return signals


def score_burst(sig: Signal, span_min: int) -> None:
    """Peak events/min vs baseline = median rate over the whole dataset span (quiet minutes count as 0).

    A signal that fires every minute (background traffic) has baseline ~= peak -> burst 0.
    A signal that is silent most of the time and then spikes has baseline 0 -> burst high.
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


# =============================================================================
# 6. CORRELATION + ROOT CAUSE
# =============================================================================

WINDOW_MIN = 5          # signals starting within this many minutes may belong together
MIN_SEVERITY = "WARN"   # ignore signals below this for correlation


def interesting(s: Signal) -> bool:
    return SEV_RANK[s.severity] >= SEV_RANK[MIN_SEVERITY] or s.burst_score >= 0.5


def correlate(signals: list[Signal], window_min: int = WINDOW_MIN) -> list[tuple[list[Signal], list[dict]]]:
    """Union-find over signals. Edge = onsets within window AND (shared entity OR both bursting)."""
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
            if abs(a.onset - b.onset) > win:
                continue
            shared = a.entities & b.entities
            why = None
            if shared:
                why = "shared entity " + ", ".join(sorted(shared)[:3])
            elif a.burst_score >= 0.5 and b.burst_score >= 0.5:
                why = "both burst in the same window"
            if why:
                edges.append({"a": a.id, "b": b.id, "why": why})
                parent[find(a.id)] = find(b.id)
    comps: dict[str, list[Signal]] = defaultdict(list)
    for s in cand:
        comps[find(s.id)].append(s)
    out = []
    for members in comps.values():
        ids = {s.id for s in members}
        out.append((sorted(members, key=lambda s: s.onset), [e for e in edges if e["a"] in ids]))
    return out


def pick_root_cause(members: list[Signal]) -> tuple[Signal, str]:
    """Earliest onset wins; ties broken by dependency words in the template, then fan-out."""
    earliest = members[0].onset
    ranked = []
    for s in members:
        lead_min = (s.onset - earliest).total_seconds() / 60
        dep = int(any(w in s.template for w in DEPENDENCY_WORDS))
        fan = sum(1 for o in members if o is not s and (o.entities & s.entities))
        score = -lead_min * 3 + dep * 1.5 + fan * 0.3 + SEV_RANK[s.severity] * 0.3
        ranked.append((score, s, lead_min, dep, fan))
    ranked.sort(key=lambda r: -r[0])
    _, s, lead, dep, fan = ranked[0]
    reasons = ["earliest onset in the group" if lead == 0 else f"starts {lead:.0f} min after the first signal"]
    if dep:
        reasons.append("mentions an infrastructure dependency (db, network, disk, latency...)")
    if fan:
        reasons.append(f"shares entities with {fan} other signal(s)")
    return s, "; ".join(reasons)


# =============================================================================
# 7. SCORING + EXPLANATION
# =============================================================================

WEIGHTS = {"burst": 0.35, "severity": 0.25, "blast_radius": 0.25, "duration": 0.15}  # tune live on sprint day


def build_incident(n: int, members: list[Signal], edges: list[dict]) -> Incident:
    root, why = pick_root_cause(members)
    services = sorted({x for s in members for x in s.services})
    hosts = sorted({x for s in members for x in s.hosts})
    start = min(s.first_seen for s in members)
    end = max(s.last_seen for s in members)
    total = sum(s.count for s in members)
    burst = max(s.burst_score for s in members)
    sev_share = sum(s.count for s in members if SEV_RANK[s.severity] >= 3) / total
    radius = min(1.0, (len(services) + len(hosts)) / 6)
    duration = min(1.0, (end - start).total_seconds() / 3600)
    raw = {"burst": burst, "severity": sev_share, "blast_radius": radius, "duration": duration}
    labels = {
        "burst": f"peak {max(s.peak_rate for s in members):.0f}/min vs baseline {max(s.baseline_rate for s in members):.0f}/min",
        "severity": f"{sev_share:.0%} of {total} events are ERROR+",
        "blast_radius": f"{len(services)} service(s), {len(hosts)} host(s)",
        "duration": f"{(end - start).total_seconds() / 60:.0f} min",
    }
    factors = [Factor(k, WEIGHTS[k], labels[k], round(WEIGHTS[k] * raw[k], 3)) for k in WEIGHTS]
    score = round(sum(f.contribution for f in factors), 3)
    top_sev = max((s.severity for s in members), key=lambda x: SEV_RANK[x])
    severity = "critical" if score >= 0.6 or top_sev == "CRITICAL" else "high" if score >= 0.4 else "medium" if score >= 0.2 else "low"
    timeline = [{"time": s.onset.isoformat(), "signal": s.id, "severity": s.severity, "count": s.count,
                 "template": s.template, "services": s.services, "role": "root cause" if s is root else "symptom"}
                for s in members]
    evidence = [ref for s in members for ref in s.evidence[:3]]
    title = f"{root.template[:70]}" + (f" ({', '.join(services[:3])})" if services else "")
    inc = Incident(id=f"INC-{n}", title=title, severity=severity, score=score, root_cause_signal=root.id,
                   root_cause_reason=why, affected_services=services, affected_hosts=hosts, started_at=start,
                   ended_at=end, signal_ids=[s.id for s in members], factors=factors, evidence=evidence,
                   timeline=timeline, narrative="")
    inc.narrative = narrative(inc, members, edges)
    return inc


def narrative(inc: Incident, members: list[Signal], edges: list[dict]) -> str:
    root = next(s for s in members if s.id == inc.root_cause_signal)
    lines = [f"{inc.id} ({inc.severity}, score {inc.score}). {sum(s.count for s in members)} raw events collapsed into "
             f"{len(members)} signal(s) between {inc.started_at:%H:%M:%S} and {inc.ended_at:%H:%M:%S}.",
             f"Probable origin: {root.id} \"{root.template}\" because: {inc.root_cause_reason}."]
    symptoms = [s for s in members if s is not root]
    if symptoms:
        lines.append("Downstream symptoms: " + "; ".join(f"{s.id} \"{s.template[:60]}\" x{s.count}" for s in symptoms[:4]) + ".")
    if edges:
        lines.append("Links: " + "; ".join(f"{e['a']}-{e['b']} ({e['why']})" for e in edges[:4]) + ".")
    lines.append("Score factors: " + ", ".join(f"{f.name} {f.contribution:.2f} ({f.value})" for f in inc.factors) + ".")
    return " ".join(lines)


def postmortem_md(inc: Incident, signals: dict[str, Signal]) -> str:
    out = [f"# Postmortem {inc.id}: {inc.title}", "",
           f"- Severity: **{inc.severity}** (score {inc.score})",
           f"- Window: {inc.started_at.isoformat()} -> {inc.ended_at.isoformat()}",
           f"- Affected services: {', '.join(inc.affected_services) or '-'}",
           f"- Affected hosts: {', '.join(inc.affected_hosts) or '-'}",
           f"- Root cause candidate: {inc.root_cause_signal} ({inc.root_cause_reason})", "",
           "## Timeline", ""]
    for t in inc.timeline:
        out.append(f"- {t['time'][11:19]} [{t['severity']}] {t['signal']} x{t['count']} {t['template']} ({t['role']})")
    out += ["", "## Why this decision", ""]
    for f in inc.factors:
        out.append(f"- {f.name}: weight {f.weight}, {f.value}, contribution {f.contribution}")
    out += ["", "## Evidence", ""]
    for sid in inc.signal_ids:
        s = signals[sid]
        for o in s.observations[:3]:
            out.append(f"- `{o.ref}` {o.message[:140]}")
    out += ["", "## Narrative", "", inc.narrative, ""]
    return "\n".join(out)


def claude_prompt(inc: Incident, signals: dict[str, Signal]) -> str:
    """Evidence bundle to paste into Claude SAKA (no API needed). Answer must cite refs."""
    ev = "\n".join(f"[{o.ref}] {o.timestamp:%H:%M:%S} {o.severity} {o.service} {o.message[:160]}"
                   for sid in inc.signal_ids for o in signals[sid].observations[:5])
    return (f"You are an SRE. Below is evidence for incident {inc.id}. Root cause candidate: {inc.root_cause_signal} "
            f"({inc.root_cause_reason}). Write (1) a 3-sentence summary, (2) the most likely root cause, "
            f"(3) three concrete actions. Cite evidence refs in [brackets]; do not invent facts.\n\nEVIDENCE\n{ev}\n")


# =============================================================================
# 8. PIPELINE
# =============================================================================

class Analysis:
    def __init__(self, observations: list[Observation], report: list[dict]):
        self.observations = observations
        self.report = report
        fingerprint(observations)
        self.signals = sorted(build_signals(observations), key=lambda s: (-s.burst_score, -SEV_RANK[s.severity], -s.count))
        comps = [(m, e) for m, e in correlate(self.signals)
                 if len(m) > 1 or SEV_RANK[m[0].severity] >= 3 or m[0].burst_score >= 0.5]
        incs = [build_incident(i, m, e) for i, (m, e) in enumerate(comps, 1)]
        incs.sort(key=lambda i: -i.score)
        for n, inc in enumerate(incs, 1):
            inc.id = f"INC-{n}"
            inc.narrative = inc.narrative.replace(inc.narrative.split(" ")[0], inc.id, 1)
        self.incidents = incs
        self.signal_by_id = {s.id: s for s in self.signals}

    def overview(self) -> dict:
        sev = Counter(o.severity for o in self.observations)
        return {"raw_events": len(self.observations), "signals": len(self.signals),
                "incidents": len(self.incidents), "files": self.report, "severity": dict(sev),
                "reduction": round(len(self.observations) / max(len(self.signals), 1), 1),
                "time_start": self.observations[0].timestamp.isoformat() if self.observations else None,
                "time_end": self.observations[-1].timestamp.isoformat() if self.observations else None,
                "timeline": [{"minute": k.isoformat(), "count": v} for k, v in sorted(
                    Counter(o.timestamp.replace(second=0, microsecond=0) for o in self.observations).items())]}


def analyze_path(path: str, mapping: dict | None = None) -> Analysis:
    obs, report = ingest(iter_path(Path(path)), mapping)
    return Analysis(obs, report)


def analyze_bytes(name: str, data: bytes, mapping: dict | None = None) -> Analysis:
    obs, report = ingest(iter_files(name, data), mapping)
    return Analysis(obs, report)


def signal_json(s: Signal) -> dict:
    return {"id": s.id, "template": s.template, "severity": s.severity, "count": s.count, "services": s.services,
            "hosts": s.hosts, "first_seen": s.first_seen.isoformat(), "last_seen": s.last_seen.isoformat(),
            "onset": s.onset.isoformat(), "peak_rate": s.peak_rate, "baseline_rate": s.baseline_rate,
            "burst_score": s.burst_score, "evidence": s.evidence, "sample": s.observations[0].message[:200]}


def incident_json(inc: Incident) -> dict:
    d = asdict(inc)
    d["started_at"], d["ended_at"] = inc.started_at.isoformat(), inc.ended_at.isoformat()
    return d


# =============================================================================
# 9. ACTION TRACKER (SQLite)
# =============================================================================

class ActionStore:
    STATUSES = ("open", "in_progress", "done")

    def __init__(self, path: str = "actions.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, title TEXT, status TEXT DEFAULT 'open',
            assignee TEXT DEFAULT '', note TEXT DEFAULT '', created_at TEXT, updated_at TEXT)""")
        self.conn.commit()

    def list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM actions ORDER BY id")]

    def create(self, incident_id: str, title: str, assignee: str = "", note: str = "") -> dict:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cur = self.conn.execute("INSERT INTO actions (incident_id,title,assignee,note,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                (incident_id, title, assignee, note, now, now))
        self.conn.commit()
        return self.get(cur.lastrowid)

    def update(self, action_id: int, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("title", "status", "assignee", "note") and v is not None}
        if "status" in allowed and allowed["status"] not in self.STATUSES:
            raise ValueError("bad status")
        allowed["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        sets = ", ".join(f"{k}=?" for k in allowed)
        self.conn.execute(f"UPDATE actions SET {sets} WHERE id=?", (*allowed.values(), action_id))
        self.conn.commit()
        return self.get(action_id)

    def delete(self, action_id: int) -> None:
        self.conn.execute("DELETE FROM actions WHERE id=?", (action_id,))
        self.conn.commit()

    def get(self, action_id: int) -> dict:
        r = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if r is None:
            raise KeyError(action_id)
        return dict(r)


# =============================================================================
# 10. WEB DASHBOARD (stdlib http.server + embedded single page)
# =============================================================================

HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Signal Sprint</title>
<style>
:root{--bg:#0b0d10;--panel:#15181d;--line:#262b33;--fg:#e6e8eb;--mut:#8b93a1;--acc:#3ddc84;--red:#ff5c5c;--org:#ffb347;--yel:#f2d55c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
.lay{display:flex;min-height:100vh}.side{width:190px;padding:18px;border-right:1px solid var(--line)}
.side h1{font-size:18px;color:var(--acc);margin:0 0 14px}.side a{display:block;color:var(--mut);padding:6px 8px;border-radius:6px;cursor:pointer}
.side a.on{color:var(--fg);background:var(--panel)}.main{flex:1;padding:22px 30px;max-width:1200px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.kpis{display:flex;gap:12px;flex-wrap:wrap}.kpi{flex:1;min-width:140px}.kpi b{display:block;font-size:30px}.kpi span{color:var(--mut)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500}tr.row:hover{background:#1b1f26;cursor:pointer}
.sev{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}.CRITICAL,.critical{background:var(--red);color:#000}
.ERROR,.high{background:var(--org);color:#000}.WARN,.medium{background:var(--yel);color:#000}.INFO,.low,.DEBUG{background:#3a4250}
.bar{height:10px;background:var(--acc);border-radius:5px}.fac{display:grid;grid-template-columns:110px 1fr 60px 1fr;gap:8px;align-items:center;margin:6px 0}
code{background:#0f1216;padding:1px 5px;border-radius:4px;font-size:13px}pre{white-space:pre-wrap;background:#0f1216;padding:12px;border-radius:8px;max-height:400px;overflow:auto}
button{background:var(--acc);color:#000;border:0;padding:7px 12px;border-radius:6px;cursor:pointer;font-weight:600}
button.sec{background:#2b3038;color:var(--fg)}input,select{background:#0f1216;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px}
.kan{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.col h3{margin:0 0 8px;color:var(--mut);font-size:13px;text-transform:uppercase}
.act{background:#0f1216;border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px}.act small{color:var(--mut)}
.drop{border:2px dashed var(--line);border-radius:10px;padding:40px;text-align:center;color:var(--mut)}
.tl{border-left:2px solid var(--line);margin-left:8px;padding-left:16px}.tl div{margin-bottom:8px;position:relative}
.tl div:before{content:"";position:absolute;left:-21px;top:8px;width:8px;height:8px;border-radius:4px;background:var(--mut)}.tl div.root:before{background:var(--red)}
.ev{cursor:pointer;color:var(--acc)}.muted{color:var(--mut)}.spark{display:flex;align-items:flex-end;gap:1px;height:60px}.spark i{flex:1;background:var(--acc);opacity:.7}
</style></head><body><div class="lay"><nav class="side"><h1>Signal Sprint</h1>
<a data-v="upload">Upload</a><a data-v="overview">Overview</a><a data-v="signals">Signals</a><a data-v="incidents">Incidents</a><a data-v="actions">Actions</a></nav>
<main class="main" id="m"></main></div>
<script>
const $=s=>document.querySelector(s);const api=(p,o)=>fetch('/api'+p,o).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sev=s=>`<span class="sev ${s}">${s}</span>`;const t=s=>s?s.slice(11,19):'';
let view='upload',cur=null;
document.querySelectorAll('.side a').forEach(a=>a.onclick=()=>go(a.dataset.v));
function go(v,arg){view=v;cur=arg;document.querySelectorAll('.side a').forEach(a=>a.classList.toggle('on',a.dataset.v===v));render()}
async function render(){const m=$('#m');try{
if(view==='upload'){m.innerHTML=`<h2>Upload dataset</h2><div class="card"><div class="drop" id="d">Drop a file / ZIP here or <input type="file" id="f"></div>
<p class="muted">Formats: JSON, JSONL, CSV/TSV, syslog, key=value, plain text. Everything becomes a canonical Observation.</p></div><div class="card" id="rep"></div>`;
const up=async f=>{$('#rep').innerHTML='Analyzing...';const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});const o=await r.json();
$('#rep').innerHTML=`<h3>Ingest report</h3><table><tr><th>File</th><th>Format</th><th>Confidence</th><th>Rows</th></tr>${o.files.map(x=>`<tr><td>${esc(x.file)}</td><td>${x.format}</td><td>${x.confidence}</td><td>${x.rows}</td></tr>`).join('')}</table><p><button onclick="go('overview')">Open overview</button></p>`};
$('#f').onchange=e=>up(e.target.files[0]);const d=$('#d');d.ondragover=e=>{e.preventDefault()};d.ondrop=e=>{e.preventDefault();up(e.dataTransfer.files[0])};
const o=await api('/overview');if(o.raw_events)$('#rep').innerHTML=`<p>Loaded dataset: ${o.raw_events} events in ${o.files.length} file(s). <button onclick="go('overview')">Open overview</button></p>`;}
else if(view==='overview'){const o=await api('/overview');const mx=Math.max(1,...o.timeline.map(x=>x.count));
m.innerHTML=`<h2>Overview</h2><div class="card kpis"><div class="kpi"><b>${o.raw_events}</b><span>raw events</span></div><div class="kpi"><b>${o.signals}</b><span>signals</span></div><div class="kpi"><b>${o.incidents}</b><span>incident candidates</span></div><div class="kpi"><b>${o.reduction}x</b><span>noise reduction</span></div></div>
<div class="card"><h3>Events per minute</h3><div class="spark">${o.timeline.map(x=>`<i title="${t(x.minute)} ${x.count}" style="height:${100*x.count/mx}%"></i>`).join('')}</div><p class="muted">${esc(o.time_start)} → ${esc(o.time_end)}</p></div>
<div class="card"><h3>Severity mix</h3>${Object.entries(o.severity).map(([k,v])=>`${sev(k)} ${v} `).join(' ')}</div>
<div class="card"><h3>Files</h3><table><tr><th>File</th><th>Format</th><th>Rows</th></tr>${o.files.map(x=>`<tr><td>${esc(x.file)}</td><td>${x.format}</td><td>${x.rows}</td></tr>`).join('')}</table></div>`}
else if(view==='signals'){const s=await api('/signals');m.innerHTML=`<h2>Signals <span class="muted">(${s.length})</span></h2><div class="card"><table><tr><th>ID</th><th>Sev</th><th>Template</th><th>Count</th><th>Burst</th><th>Peak/base</th><th>Services</th><th>Onset</th></tr>
${s.map(x=>`<tr class="row" onclick="go('signal','${x.id}')"><td>${x.id}</td><td>${sev(x.severity)}</td><td><code>${esc(x.template)}</code></td><td>${x.count}</td><td><div class="bar" style="width:${Math.max(4,x.burst_score*100)}px"></div></td><td>${x.peak_rate}/${x.baseline_rate}</td><td>${x.services.join(', ')}</td><td>${t(x.onset)}</td></tr>`).join('')}</table></div>`}
else if(view==='signal'){const s=await api('/signals/'+cur);m.innerHTML=`<h2>${s.id} ${sev(s.severity)}</h2><div class="card"><p><code>${esc(s.template)}</code></p><p>${s.count} events, peak ${s.peak_rate}/min, baseline ${s.baseline_rate}/min, burst ${s.burst_score}</p><p>Services: ${s.services.join(', ')||'-'} · Hosts: ${s.hosts.join(', ')||'-'}</p></div>
<div class="card"><h3>Evidence</h3>${s.observations.map(o=>`<div><code>${esc(o.ref)}</code> ${t(o.timestamp)} ${esc(o.message)}</div>`).join('')}</div><button class="sec" onclick="go('signals')">Back</button>`}
else if(view==='incidents'){const l=await api('/incidents');m.innerHTML=`<h2>Incident candidates <span class="muted">(${l.length})</span></h2><div class="card"><table><tr><th>ID</th><th>Sev</th><th>Score</th><th>Title</th><th>Signals</th><th>Services</th><th>Window</th></tr>
${l.map(x=>`<tr class="row" onclick="go('incident','${x.id}')"><td>${x.id}</td><td>${sev(x.severity)}</td><td><div class="bar" style="width:${x.score*100}px"></div> ${x.score}</td><td>${esc(x.title)}</td><td>${x.signal_ids.length}</td><td>${x.affected_services.join(', ')}</td><td>${t(x.started_at)}–${t(x.ended_at)}</td></tr>`).join('')}</table></div>`}
else if(view==='incident'){const i=await api('/incidents/'+cur);const acts=(await api('/actions')).filter(a=>a.incident_id===i.id);
m.innerHTML=`<h2>${i.id} ${sev(i.severity)} <span class="muted">score ${i.score}</span></h2><div class="card"><h3>${esc(i.title)}</h3><p>${esc(i.narrative)}</p><p><b>Root cause candidate:</b> ${i.root_cause_signal} — ${esc(i.root_cause_reason)}</p></div>
<div class="card"><h3>Why this score</h3>${i.factors.map(f=>`<div class="fac"><span>${f.name}</span><div class="bar" style="width:${f.contribution*300}px"></div><span>${f.contribution}</span><span class="muted">${esc(f.value)} × w${f.weight}</span></div>`).join('')}</div>
<div class="card"><h3>Timeline</h3><div class="tl">${i.timeline.map(x=>`<div class="${x.role==='root cause'?'root':''}"><b>${t(x.time)}</b> ${sev(x.severity)} <a class="ev" onclick="go('signal','${x.signal}')">${x.signal}</a> ×${x.count} <code>${esc(x.template)}</code> <span class="muted">${x.role}</span></div>`).join('')}</div></div>
<div class="card"><h3>Evidence</h3>${i.evidence.map(e=>`<code class="ev" onclick="showEv('${esc(e)}')">${esc(e)}</code> `).join('')}<pre id="evbox" class="muted">click a reference to see the raw line</pre></div>
<div class="card"><h3>Actions (${acts.length})</h3>${acts.map(a=>`<div class="act">${esc(a.title)} <small>${a.status} · ${esc(a.assignee)}</small></div>`).join('')}
<p><input id="at" placeholder="new action" size="40"> <input id="aa" placeholder="assignee" size="12"> <button onclick="addAct('${i.id}')">Add</button></p></div>
<p><a href="/api/incidents/${i.id}/postmortem" target="_blank"><button>Export postmortem</button></a> <button class="sec" onclick="copyPrompt('${i.id}')">Copy Claude prompt</button> <button class="sec" onclick="go('incidents')">Back</button></p>`}
else if(view==='actions'){const l=await api('/actions');const cols={open:'Open',in_progress:'In progress',done:'Done'};
m.innerHTML=`<h2>Actions</h2><div class="kan">${Object.entries(cols).map(([k,n])=>`<div class="col card"><h3>${n} (${l.filter(a=>a.status===k).length})</h3>${l.filter(a=>a.status===k).map(a=>`<div class="act"><b>${esc(a.title)}</b><br><small><a class="ev" onclick="go('incident','${a.incident_id}')">${a.incident_id}</a> · ${esc(a.assignee)||'unassigned'} · ${a.updated_at.slice(0,16)}</small><br>
<select onchange="setAct(${a.id},this.value)">${Object.keys(cols).map(s=>`<option ${s===a.status?'selected':''}>${s}</option>`).join('')}</select> <button class="sec" onclick="delAct(${a.id})">✕</button></div>`).join('')}</div>`).join('')}</div>`}
}catch(e){m.innerHTML=`<div class="card">No data yet. <a class="ev" onclick="go('upload')">Upload a dataset</a>. (${e.message})</div>`}}
async function showEv(ref){const o=await api('/evidence/'+encodeURIComponent(ref));$('#evbox').textContent=`${o.ref}  ${o.timestamp}  ${o.severity}  ${o.service}\n${o.message}\n${JSON.stringify(o.attributes)}`}
async function addAct(inc){await api('/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({incident_id:inc,title:$('#at').value,assignee:$('#aa').value})});render()}
async function setAct(id,st){await api('/actions/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:st})});render()}
async function delAct(id){await api('/actions/'+id,{method:'DELETE'});render()}
async function copyPrompt(id){const p=await api('/incidents/'+id+'/prompt');await navigator.clipboard.writeText(p.prompt);alert('Prompt copied. Paste it into Claude SAKA.')}
render();
</script></body></html>"""


class App:
    def __init__(self, analysis: Analysis | None, store: ActionStore):
        self.analysis = analysis
        self.store = store


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def send(self, code: int, body: Any, ctype: str = "application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json" else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def need(self):
            if app.analysis is None:
                self.send(404, {"error": "no dataset loaded"})
                return None
            return app.analysis

        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            if p == "/" or not p.startswith("/api/"):
                return self.send(200, HTML, "text/html")
            a = app.analysis
            if p == "/api/actions":
                return self.send(200, app.store.list())
            if (a := self.need()) is None:
                return
            if p == "/api/overview":
                return self.send(200, a.overview())
            if p == "/api/signals":
                return self.send(200, [signal_json(s) for s in a.signals])
            if p.startswith("/api/signals/"):
                s = a.signal_by_id.get(p.split("/")[3])
                if not s:
                    return self.send(404, {"error": "unknown signal"})
                d = signal_json(s)
                d["observations"] = [{"ref": o.ref, "timestamp": o.timestamp.isoformat(), "message": o.message} for o in s.observations[:50]]
                return self.send(200, d)
            if p == "/api/incidents":
                return self.send(200, [incident_json(i) for i in a.incidents])
            if p.startswith("/api/incidents/"):
                parts = p.split("/")
                inc = next((i for i in a.incidents if i.id == parts[3]), None)
                if not inc:
                    return self.send(404, {"error": "unknown incident"})
                if len(parts) > 4 and parts[4] == "postmortem":
                    return self.send(200, postmortem_md(inc, a.signal_by_id), "text/markdown")
                if len(parts) > 4 and parts[4] == "prompt":
                    return self.send(200, {"prompt": claude_prompt(inc, a.signal_by_id)})
                return self.send(200, incident_json(inc))
            if p.startswith("/api/evidence/"):
                from urllib.parse import unquote
                ref = unquote(p[len("/api/evidence/"):])
                o = next((o for o in a.observations if o.ref == ref), None)
                if not o:
                    return self.send(404, {"error": "unknown ref"})
                return self.send(200, {"ref": o.ref, "timestamp": o.timestamp.isoformat(), "severity": o.severity,
                                       "service": o.service, "host": o.host, "message": o.message, "attributes": o.attributes})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/upload":
                name = parse_qs(u.query).get("name", ["upload.log"])[0]
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                app.analysis = analyze_bytes(name, data)
                return self.send(200, app.analysis.overview())
            if u.path == "/api/actions":
                b = self.body_json()
                if not b.get("title"):
                    return self.send(400, {"error": "title required"})
                return self.send(201, app.store.create(b.get("incident_id", ""), b["title"], b.get("assignee", ""), b.get("note", "")))
            self.send(404, {"error": "not found"})

        def do_PATCH(self):
            p = urlparse(self.path).path
            if p.startswith("/api/actions/"):
                try:
                    return self.send(200, app.store.update(int(p.split("/")[3]), **self.body_json()))
                except (KeyError, ValueError) as e:
                    return self.send(400, {"error": str(e)})
            self.send(404, {"error": "not found"})

        def do_DELETE(self):
            p = urlparse(self.path).path
            if p.startswith("/api/actions/"):
                app.store.delete(int(p.split("/")[3]))
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})

    return H


def serve(path: str | None, port: int, db: str) -> None:
    app = App(analyze_path(path) if path else None, ActionStore(db))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    print(f"Signal Sprint dashboard: http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# =============================================================================
# 11. CLI: inspect / analyze / serve
# =============================================================================

def inspect(path: str, sample_lines: int = 5) -> None:
    for fname, text in iter_path(Path(path)):
        lines = text.splitlines()
        fmt, conf = detect_format(text)
        recs = list(PARSERS[fmt](text))
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
        roles = resolve_roles(keys)
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


def print_analysis(a: Analysis, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({"overview": a.overview(), "signals": [signal_json(s) for s in a.signals],
                          "incidents": [incident_json(i) for i in a.incidents]}, indent=2, default=str))
        return
    o = a.overview()
    print(f"# Analysis\n\n{o['raw_events']} raw events -> {o['signals']} signals -> {o['incidents']} incident candidates "
          f"({o['reduction']}x reduction)\n")
    print("## Top signals\n")
    for s in a.signals[:15]:
        print(f"- {s.id} [{s.severity}] x{s.count} burst {s.burst_score} peak {s.peak_rate:.0f}/min  `{s.template[:90]}`  {', '.join(s.services)}")
    print("\n## Incidents\n")
    for inc in a.incidents:
        print(f"### {inc.id} [{inc.severity}] score {inc.score}: {inc.title}\n")
        print(inc.narrative + "\n")
        for t in inc.timeline:
            print(f"- {t['time'][11:19]} {t['signal']} [{t['severity']}] x{t['count']} {t['template'][:80]} ({t['role']})")
        print(f"- evidence: {', '.join(inc.evidence[:8])}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="describe an unknown dataset")
    p.add_argument("path")
    p.add_argument("--lines", type=int, default=5)
    p = sub.add_parser("analyze", help="run the pipeline and print results")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("serve", help="web dashboard")
    p.add_argument("path", nargs="?")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default="actions.db")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        inspect(args.path, args.lines)
    elif args.cmd == "analyze":
        print_analysis(analyze_path(args.path), "json" if args.json else "md")
    else:
        serve(args.path, args.port, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
