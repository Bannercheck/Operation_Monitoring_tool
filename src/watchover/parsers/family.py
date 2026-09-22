"""Log families: which product wrote the file. Detected from a sample of lines; the family name becomes the default
service when a record carries none (a PostgreSQL log is the postgres service even though no line says so)."""

from __future__ import annotations

import re

# (family / default service, patterns); a family wins when >= 30 % of the sampled lines match, or a strong marker appears
FAMILIES: list[tuple[str, list[re.Pattern]]] = [
    ("postgres", [re.compile(r"\] (?:[\w@]+ )?(?:LOG|ERROR|FATAL|DETAIL|STATEMENT|HINT|WARNING|PANIC):  ")]),
    ("mysql", [re.compile(r"\[MY-\d{6}\]"), re.compile(r"\bmysqld\b")]),
    ("sqlserver", [re.compile(r"^\S+ \S+ +(?:spid\d+s?|Logon|Backup|Server) +"), re.compile(r"Microsoft SQL Server")]),
    ("oracle-ebs", [re.compile(r"\bAPP-FND-\d{5}\b|\bConcurrent Manager\b|\*\*Starts\*\*|\bRequest ID:|\bFNDLIBR\b|\bAPPLCSF\b|\bOACORE\b|\bWLS_FORMS\b")]),
    ("oracle", [re.compile(r"\bORA-\d{5}\b|\bTNS-\d{5}\b|\bLGWR\b|\bARC\d\b|\balert_\w+\.log\b|advanced to log sequence")]),
    ("dynamics-ax", [re.compile(r"Microsoft\.Dynamics\.AX|\bAx32Serv\b|\bAOS\b.*(?:session|instance)|Dynamics AX")]),
    ("dynamics-365", [re.compile(r"Microsoft\.Dynamics\.(?:365|Ax\.Xpp|Platform)|\bD365\b|\bSysOperation\w*\b|Dynamics 365|\bBatchJob\b|\bDMF\w*\b")]),
    ("dynamics-nav", [re.compile(r"Microsoft Dynamics NAV|Business Central|\bNavServer\b|\bMicrosoft\.Dynamics\.Nav\b|\bNAV Server\b|\bBC\d{2}\b")]),
    ("sap", [re.compile(r"\bSAP\b|\bABAP\b|\bNetWeaver\b|\bHANA\b|\bSM21\b|\bdisp\+work\b")]),
    ("redis", [re.compile(r"^\d+:[MCSX] \d{2} \w{3} \d{4}")]),
    ("nginx", [re.compile(r"\bnginx\b|^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[\w+\] \d+#\d+:")]),
    ("apache", [re.compile(r"\[(?:core|mpm_\w+|ssl|proxy\w*|rewrite):\w+\] \[pid \d+")]),
    ("kafka", [re.compile(r"\(kafka\.[\w.]+\)|\bKafkaServer\b|\bReplicaManager\b")]),
    ("zookeeper", [re.compile(r"\(org\.apache\.zookeeper")]),
    ("elasticsearch", [re.compile(r"\[o\.e\.[\w.]+\]|\bElasticsearch\b")]),
    ("haproxy", [re.compile(r"\bhaproxy\[\d+\]")]),
    ("postfix", [re.compile(r"\bpostfix/\w+\[\d+\]")]),
    ("sshd", [re.compile(r"\bsshd\[\d+\]")]),
    ("iis", [re.compile(r"^#Software: Microsoft Internet Information Services")]),
    ("windows", [re.compile(r"<Event xmlns=|EventID|Event ID")]),
    ("java", [re.compile(r"^\s+at [\w$.]+\([\w.]*:\d+\)|^[\w.]+Exception: |\bcommon frames omitted\b")]),
    ("python", [re.compile(r"^Traceback \(most recent call last\):|^  File \"[^\"]+\", line \d+")]),
    ("dotnet", [re.compile(r"^\s+at [\w.]+\.[\w`]+\(.*\) in .*\.cs:line \d+|^System\.[\w.]+Exception")]),
    ("golang", [re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w+\.go:\d+:|^goroutine \d+ \[")]),
]
STRONG = {"oracle-ebs", "dynamics-ax", "dynamics-365", "dynamics-nav", "sap", "iis", "redis", "postgres", "mysql", "sqlserver", "kafka", "haproxy", "postfix", "java", "python", "dotnet", "golang"}


import contextvars

FAMILY_HINT: contextvars.ContextVar = contextvars.ContextVar("watchover_family_hint", default=None)   # set by the pipeline when the source's family is already known
FAMILY_SEEN: contextvars.ContextVar = contextvars.ContextVar("watchover_family_seen", default=None)   # what the last detection decided (read back by the pipeline)


def detect_family(lines: list[str]) -> str:
    hinted = FAMILY_HINT.get()
    if hinted is not None:
        FAMILY_SEEN.set(hinted)
        return hinted
    fam = _detect_family(lines)
    FAMILY_SEEN.set(fam)
    return fam


def _detect_family(lines: list[str]) -> str:
    """Family with the most matching lines among the first 200; '' when nothing reaches the threshold."""
    sample = [ln for ln in lines[:200] if ln.strip()]
    if not sample:
        return ""
    best, best_hits = "", 0
    for fam, pats in FAMILIES:
        hits = sum(1 for ln in sample if any(p.search(ln) for p in pats))
        if hits > best_hits:
            best, best_hits = fam, hits
    if not best:
        return ""
    if best in STRONG and best_hits >= 1:
        return best
    return best if best_hits / len(sample) >= 0.3 else ""
