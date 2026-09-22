"""Format detector: looks at the first 200 non-empty lines and picks json / jsonl / csv / syslog / kv / text."""

from __future__ import annotations

import json
import re

_SYSLOG_TS = r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}(?:\s\d{4})?\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)"      # BSD (optionally with year: Cisco) or ISO
_NOT_LEVEL = r"(?!(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERR|ERROR|CRIT|CRITICAL|FATAL|SEVERE|INF|WRN|FTL|DBG)\b)"
SYSLOG_RE = re.compile(r"^(?:<(?P<pri>\d+)>)?" + _SYSLOG_TS + r"\s+(?P<host>" + _NOT_LEVEL + r"\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")
SYSLOG_NOPROG_RE = re.compile(r"^(?:<(?P<pri>\d+)>)?" + _SYSLOG_TS + r"\s+(?P<host>" + _NOT_LEVEL + r"[A-Za-z][\w.\-]*)\s+(?P<msg>.*)$")       # header without 'program:'
RFC5424_RE = re.compile(r"^<(?P<pri>\d+)>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<pid>\S+) (?P<msgid>\S+) (?P<sd>-|(?:\[.*?\])+) ?(?P<msg>.*)$")
CEF_RE = re.compile(r"^(?:.*?\s)?(?:CEF:\d+\||LEEF:\d+(?:\.\d+)?\|)")
CLF_RE = re.compile(r'^\S+ \S+ \S+ \[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+\-]\d{4}\] "')
ALB_RE = re.compile(r"^(?:https?|h2|ws|wss|tls|tcp) \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z \S+ ")
WINEVT_RE = re.compile(r"^\s*<Event\b[^>]*xmlns=")
W3C_RE = re.compile(r"^#Fields:\s")
CRI_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z (?:stdout|stderr) [FP] ")
KV_RE = re.compile(r'([\w.\-@]+)=("(?:[^"\\]|\\.)*"|\S+)')
DELIMS = ("\t", ",", ";", "|")
SAMPLE_LINES = 200


def detect_delimiter(lines: list[str]) -> str | None:
    """Quote-aware: field counts come from csv.reader, so commas inside quoted messages do not break the check."""
    import csv
    for d in DELIMS:
        try:
            counts = [len(row) for row in csv.reader(lines[:30], delimiter=d)]
        except csv.Error:
            continue
        if counts and min(counts) >= 2 and len(set(counts)) <= 2:
            return d
    return None


def detect_format(text: str) -> tuple[str, float]:
    """Return (format, confidence in 0..1)."""
    lines = [ln for ln in text[:400_000].splitlines()[:SAMPLE_LINES] if ln.strip()]      # a prefix is enough; never split a 300 MB file here
    if not lines:
        return "text", 0.0
    first = lines[0].lstrip()
    if first.startswith("[") or (first.startswith("{") and (len(lines) == 1 or not first.rstrip().endswith("}"))):
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
        return "jsonl", round(ok / len(lines), 2)
    if any(W3C_RE.match(ln) for ln in lines[:10]):
        return "w3c", 0.95
    if sum(1 for ln in lines if WINEVT_RE.match(ln)) / len(lines) > 0.6:
        return "winevt", 0.95
    cef = sum(1 for ln in lines if CEF_RE.match(ln))
    if cef / len(lines) > 0.6:
        return "cef", round(cef / len(lines), 2)
    acc = sum(1 for ln in lines if CLF_RE.match(ln) or ALB_RE.match(ln))
    if acc / len(lines) > 0.6:
        return "access", round(acc / len(lines), 2)
    if sum(1 for ln in lines if CRI_LINE_RE.match(ln)) / len(lines) > 0.6:                 # kubernetes container runtime: "ts stream F line"
        return "text", 0.9
    hits = sum(1 for ln in lines if SYSLOG_RE.match(ln) or RFC5424_RE.match(ln))
    if hits / len(lines) > 0.6:
        return "syslog", round(hits / len(lines), 2)
    hits2 = sum(1 for ln in lines if SYSLOG_NOPROG_RE.match(ln))
    if (hits + hits2) / len(lines) > 0.6:                                                 # BSD header without 'prog:' (PAN-OS CSV, some appliances)
        return "syslog", round((hits + hits2) / len(lines), 2)
    from .parsers.sap_parser import sap_score          # SAP families (dev traces, HANA, NW Java, JUL, GC, tp, SM21)
    sap = sap_score(lines)
    if sap >= 0.5:
        return "sap", sap
    d = detect_delimiter(lines)
    if d and len(lines) >= 2:
        import csv as _csv
        header = next(_csv.reader([lines[0]], delimiter=d))
        alpha = sum(1 for h in header if h.strip().replace("_", "").replace(" ", "").isalpha())
        if alpha / len(header) > 0.6:
            return "csv", 0.9
    kv = sum(1 for ln in lines if len(KV_RE.findall(ln)) >= 2)
    if kv / len(lines) > 0.6:
        return "kv", round(kv / len(lines), 2)
    return "text", 0.5
