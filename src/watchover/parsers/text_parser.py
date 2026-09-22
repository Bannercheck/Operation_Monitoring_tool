"""Generic text lines: leading timestamp (30+ shapes), level, logger / bracketed app name, rest is the message.
Untimestamped lines fold into the previous event (stack traces, ORA- blocks, wrapped messages) when the file is
timestamp-oriented; container runtime (CRI) and Redis prefixes are stripped first; the log family supplies the service."""

from __future__ import annotations

import re
from typing import Iterator

from .base import Parser
from .family import detect_family

MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
DOW = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
TS_RE = re.compile(r"^\[?("
                   r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?"       # ISO, PostgreSQL ' +03', Serilog ' +03:00'
                   r"|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"                                             # 2026/09/16 02:14:07 (Go, nginx)
                   r"|\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?"                      # 16.09.2026 02:14:07 · 09/16/2026 02:14:07 PM
                   r"|\d{1,2}-" + MON.upper() + r"-\d{4} \d{2}:\d{2}:\d{2}"                                        # 22-SEP-2026 09:14:02 (Oracle EBS)
                   r"|\d{1,2}-" + MON + r"-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
                   r"|\d{2} " + MON + r" \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?"                                        # 22 Sep 2026 09:14:02.003 (Redis)
                   r"|" + MON + r" \d{1,2} \d{4} \d{2}:\d{2}:\d{2}"                                                 # Sep 22 2026 09:14:02 (CEF rt, Cisco)
                   r"|" + DOW + r" " + MON + r" +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}"                                   # Wed Sep 16 02:14:07 2026
                   r"|" + DOW + r", \d{1,2} " + MON + r" \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?"                 # RFC 2822
                   r"|" + MON + r" \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)"                                   # Sep 16, 2026 2:14:07 AM
                   r"|\d{8}T\d{6}(?:\.\d+)?Z?"                                                                    # 20260922T091700Z
                   r"|\d{10}(?:\.\d{1,6})?(?=\s)"                                                                 # epoch seconds
                   r"|\d{13}(?=\s)"                                                                               # epoch milliseconds
                   r")\]?:?\s*")
LEVEL_RE = re.compile(r"^\[?(TRACE|DEBUG|INFO|INFORMATION|NOTICE|NOTE|SYSTEM|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|SEVERE|EMERG(?:ENCY)?|ALERT|PANIC|CONFIG|FINE(?:R|ST)?|INF|WRN|FTL|DBG|VRB)\]?[:\s]+", re.I)
LOGGER_RE = re.compile(r"^\[?([A-Za-z][\w.\-]{2,})\]?:\s+")
APP_BRACKET_RE = re.compile(r"^\[([A-Za-z][\w.\-]{1,40})\]\s*")          # Spring / Kafka style "[payment-api]"
THREAD_BRACKET_RE = re.compile(r"^\[[^\]]{1,60}\]\s*")                    # "[http-nio-8080-exec-12]" is not a service
THREAD_ID_RE = re.compile(r"^\d+\s+(?=\[)")                               # MySQL "12 [Warning]"
CODE_RE = re.compile(r"^[A-Z]{2,5}-\d{3,7}$")
KV_TS_RE = re.compile(r'^(?:time|ts|timestamp|date|@timestamp)="?([^"\s]+)"?\s+')
CRI_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (stdout|stderr) ([FP]) ")
REDIS_RE = re.compile(r"^(\d+):([MCSX]) (?=\d{2} [A-Z][a-z]{2} \d{4})")
REDIS_MARK = {"#": "WARN", "*": "INFO", "-": "INFO", ".": "DEBUG"}
CONT_RE = re.compile(r"^(\s|\tat |at [\w$.]+\(|\.\.\. \d+ (?:more|common frames)|Caused by:|Traceback \(most|  File \"|[\w$.]+(?:Exception|Error)(?::|\b)|System\.[\w.]+:|ORA-\d{5}|APP-FND-\d{5}|\*\*\*|\+-{10,}|-{10,}$)")
EMBEDDED_TS_RE = re.compile(r"(?:\*\*(?:Starts|Ends)\*\*|Current system time is |^\[)?(\d{2}-[A-Z]{3}-\d{4} \d{2}:\d{2}:\d{2}(?::\d{3})?)\]?")   # Oracle EBS request / FND logs
APP_NAME_RE = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?=[\s:])")                                          # "payment-api gateway timeout": hyphenated app name
PG_RE = re.compile(r"^\[\d+(?:-\d+)?\] (?:[\w.\-]+@[\w.\-]+ )?(LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|INFO|DEBUG\d?|DETAIL|STATEMENT|HINT|CONTEXT|QUERY):\s+")   # PostgreSQL
PG_LEVEL = {"LOG": "INFO", "DETAIL": "DETAIL", "STATEMENT": "STATEMENT", "HINT": "HINT", "CONTEXT": "CONTEXT", "QUERY": "STATEMENT"}
SEV_NUM_RE = re.compile(r"\bSeverity: (\d{1,2})\b")                                                        # SQL Server "Error: 18456, Severity: 14, State: 8."
DYNAMICS_RE = re.compile(r"^(?:Microsoft\.Dynamics\.[\w.]+|Ax32Serv|NavServer)\s*[:\-]\s*", re.I)


def parse_head(ln: str) -> tuple[dict, str]:
    """Timestamp / level / app name from the start of a line; returns (fields, remaining text)."""
    rec: dict = {}
    rest = ln
    if m := CRI_RE.match(rest):                                   # kubernetes container runtime prefix
        rec["timestamp"], rec["stream"], rec["_partial"] = m[1], m[2], m[3] == "P"
        rest = rest[m.end():]
        if m2 := TS_RE.match(rest):                               # the app's own timestamp is more precise, keep it
            rec["timestamp"], rest = m2[1], rest[m2.end():]
    elif m := REDIS_RE.match(rest):
        rec["pid"], rec["role"] = m[1], m[2]
        rest = rest[m.end():]
        if m2 := TS_RE.match(rest):
            rec["timestamp"], rest = m2[1], rest[m2.end():]
        if rest[:1] in REDIS_MARK and rest[1:2] == " ":
            rec["severity"], rest = REDIS_MARK[rest[0]], rest[2:]
    elif m := TS_RE.match(rest):
        rec["timestamp"], rest = m[1], rest[m.end():]
    elif m := KV_TS_RE.match(rest):                               # logfmt-style line inside a text file: time="…" level=… msg=…
        rec["timestamp"] = m[1]
    if m := THREAD_ID_RE.match(rest):
        rest = rest[m.end():]
    if m := PG_RE.match(rest):
        lvl = m[1].upper()
        rec["severity"], rest = PG_LEVEL.get(lvl, lvl), rest[m.end():]
        rec["pid"] = ln.split("]")[0].lstrip("[")
    elif m := LEVEL_RE.match(rest):
        rec["severity"], rest = m[1], rest[m.end():]
        for _ in range(3):                                        # "[MY-010055] [Server]", "[payment-api] [http-nio-exec-12]"
            m = APP_BRACKET_RE.match(rest)
            if m and CODE_RE.match(m[1]):
                rec["code"], rest = m[1], rest[m.end():]
            elif m and "service" not in rec:
                rec["service"], rest = m[1], rest[m.end():]
            elif (m := THREAD_BRACKET_RE.match(rest)):
                rest = rest[m.end():]
            else:
                break
    if "severity" not in rec and (m := SEV_NUM_RE.search(rest)):
        n = int(m[1]); rec["severity"] = "CRITICAL" if n >= 19 else "ERROR" if n >= 11 else "INFO"
    if "timestamp" not in rec and (m := EMBEDDED_TS_RE.search(rest[:120])):
        rec["timestamp"] = m[1][:19] if m[1].count(":") == 3 else m[1]
        if rest.startswith("["):
            rest = rest[m.end():].strip()
    if "service" not in rec:
        if m := DYNAMICS_RE.match(rest):
            rec["service"], rest = m[0].strip(" :-"), rest[m.end():]
        elif (m := LOGGER_RE.match(rest)) and not m[1].endswith(".go"):
            rec["service"], rest = m[1], rest[m.end():]
        elif m := APP_NAME_RE.match(rest):
            rec["service"] = m[1]
        elif m := APP_BRACKET_RE.match(rest):
            rec["service"], rest = m[1], rest[m.end():]
    return rec, rest.strip()


class TextParser(Parser):
    name = "text"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        lines = text.splitlines()
        sample = [ln for ln in lines[:300] if ln.strip()]
        with_ts = sum(1 for ln in sample if "timestamp" in parse_head(ln)[0])
        fold = with_ts >= max(1, 0.3 * len(sample))               # timestamp-oriented file: untimestamped lines belong to the previous event
        family = detect_family(sample)
        prev: tuple[int, dict] | None = None
        for i, ln in enumerate(lines, 1):
            if not ln.strip():
                continue
            rec, rest = parse_head(ln)
            if rec.get("severity") in ("DETAIL", "STATEMENT", "HINT", "CONTEXT") and prev is not None:     # PostgreSQL: details of the error just above
                prev[1]["message"] += f"\n{rec['severity']}: {rest}"
                prev[1]["_lines"] = prev[1].get("_lines", 1) + 1
                continue
            if "timestamp" not in rec and prev is not None and (fold or CONT_RE.match(ln) or prev[1].get("_partial")):
                p = prev[1]
                p["message"] = (p["message"] + "\n" + ln.strip()).strip() if not p.get("_partial") else p["message"] + ln.strip()
                p["_lines"] = p.get("_lines", 1) + 1
                p["_partial"] = rec.get("_partial", False)
                continue
            rec["message"] = rest
            if family and not rec.get("service"):
                rec["service"] = family
            if prev is not None:
                yield prev
            prev = (i, rec)
        if prev is not None:
            yield prev
