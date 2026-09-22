"""HTTP access logs: Apache / nginx common & combined, AWS ALB / ELB. Status class gives the severity (5xx ERROR, 4xx WARN),
the virtual host (when present) the service, the request line the message."""

from __future__ import annotations

import re
from typing import Iterator

from .base import Parser

CLF_RE = re.compile(r'^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3}|-) (?P<bytes>\S+)'
                    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?: (?P<extra>.*))?$')
VHOST_RE = re.compile(r'^(?P<vhost>[\w.\-]+)(?::\d+)? (?=\S+ \S+ \S+ \[)')                       # "vhost:port client - user [ts] ..." (Apache vhost_combined)
ALB_RE = re.compile(r"^(?P<proto>https?|h2|ws|wss|tls|tcp) (?P<ts>\S+) (?P<elb>\S+) (?P<client>\S+) (?P<target>\S+) (?P<t_req>\S+) (?P<t_tgt>\S+) (?P<t_resp>\S+) "
                    r"(?P<status>\S+) (?P<tstatus>\S+) (?P<rbytes>\S+) (?P<sbytes>\S+) \"(?P<request>[^\"]*)\" \"(?P<agent>[^\"]*)\" \S+ \S+ (?P<tg>\S+) \"[^\"]*\" \"(?P<domain>[^\"]*)\"")


def status_severity(st: str) -> str:
    return "ERROR" if st.startswith("5") else "WARN" if st.startswith("4") else "INFO"


class AccessParser(Parser):
    name = "access"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            vhost = ""
            if v := VHOST_RE.match(ln):
                vhost, ln = v["vhost"], ln[v.end():]
            if m := CLF_RE.match(ln):
                st = m["status"]
                rec = {"timestamp": m["ts"], "client": m["client"], "request": m["request"], "status": st, "bytes": m["bytes"],
                       "message": f"{m['request']} {st}", "severity": status_severity(st), "service": vhost or "http"}
                if m["user"] and m["user"] != "-":
                    rec["user"] = m["user"]
                if m["referer"] is not None:
                    rec["referer"], rec["agent"] = m["referer"], m["agent"]
                if m["extra"]:
                    parts = m["extra"].split()
                    try:
                        rec["duration_s"] = float(parts[-1])
                    except ValueError:
                        rec["extra"] = m["extra"]
                yield i, rec
            elif m := ALB_RE.match(ln):
                st = m["status"] if m["status"] != "-" else m["tstatus"]
                rec = {"timestamp": m["ts"], "client": m["client"].split(":")[0], "target": m["target"], "request": m["request"], "status": st,
                       "message": f"{m['request']} {st}", "severity": "ERROR" if m["status"] == "-" or st.startswith("5") else status_severity(st),
                       "service": m["domain"] if m["domain"] not in ("", "-") else m["elb"], "host": m["target"].split(":")[0] if m["target"] != "-" else "",
                       "latency_s": m["t_tgt"], "agent": m["agent"]}
                yield i, rec
