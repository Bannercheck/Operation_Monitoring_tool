"""HTTP access logs: Apache / nginx common & combined (with trailing timing / request-id fields), AWS ALB / ELB, Envoy / Istio. Status class gives the severity (5xx ERROR, 4xx WARN),
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

# Envoy / Istio default access log: [START] "METHOD PATH PROTO" CODE FLAGS RX TX DURATION UPSTREAM_TIME "XFF" "UA" "X-REQUEST-ID" "AUTHORITY" "UPSTREAM_HOST" ...
ENVOY_RE = re.compile(r'^\[(?P<ts>[^\]]+)\] "(?P<request>[A-Z]+ \S+ [^"]*)" (?P<status>\d{3}|-) (?P<flags>\S+) (?:\S+ )?(?P<rx>\d+|-) (?P<tx>\d+|-) '
                      r'(?P<dur>\d+|-) (?P<up>\d+|-) "(?P<xff>[^"]*)" "(?P<agent>[^"]*)" "(?P<reqid>[^"]*)" "(?P<authority>[^"]*)" "(?P<upstream>[^"]*)"')
EXTRA_KV = re.compile(r'(?P<k>[A-Za-z_][\w.\-]*)=(?P<v>"[^"]*"|\S+)')
UUIDISH = re.compile(r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$|^[0-9a-fA-F]{16,64}$')


def parse_extra(extra: str, rec: dict) -> None:
    """Trailing fields that nginx / Apache formats commonly add after the combined part: key=value pairs (rt=0.12 req_id=...),
    bare seconds with a fraction ($request_time, $upstream_response_time), a bare integer (Apache %D, microseconds) and a bare
    request id (uuid / long hex). Anything else stays in `extra`."""
    rest = []
    for k, v in ((m["k"], m["v"].strip('"')) for m in EXTRA_KV.finditer(extra)):
        rec[k.lower()] = v
    floats = 0
    for tok in EXTRA_KV.sub(" ", extra).split():
        t = tok.strip('"')
        if re.fullmatch(r"\d+\.\d+", t):
            rec["duration_s" if floats == 0 else "upstream_duration_s"] = float(t); floats += 1
        elif re.fullmatch(r"\d+", t) and "duration_s" not in rec and "duration_us" not in rec:
            rec["duration_us"] = int(t)
        elif UUIDISH.match(t) and "request_id" not in rec:
            rec["request_id"] = t
        elif t not in ("-", ""):
            rest.append(tok)
    if rest:
        rec["extra"] = " ".join(rest)


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
                    parse_extra(m["extra"], rec)
                yield i, rec
            elif m := ALB_RE.match(ln):
                st = m["status"] if m["status"] != "-" else m["tstatus"]
                rec = {"timestamp": m["ts"], "client": m["client"].split(":")[0], "target": m["target"], "request": m["request"], "status": st,
                       "message": f"{m['request']} {st}", "severity": "ERROR" if m["status"] == "-" or st.startswith("5") else status_severity(st),
                       "service": m["domain"] if m["domain"] not in ("", "-") else m["elb"], "host": m["target"].split(":")[0] if m["target"] != "-" else "",
                       "latency_s": m["t_tgt"], "agent": m["agent"]}
                yield i, rec
            elif m := ENVOY_RE.match(ln):
                st = m["status"] if m["status"] != "-" else "0"
                rec = {"timestamp": m["ts"], "request": m["request"], "status": st, "message": f"{m['request']} {st}",
                       "severity": "ERROR" if st.startswith("5") or st == "0" else status_severity(st), "service": m["authority"].split(":")[0] if m["authority"] not in ("", "-") else "envoy",
                       "envoy.flags": m["flags"], "agent": m["agent"], "ts_is_start": True}
                if m["dur"] != "-":
                    rec["duration_ms"] = int(m["dur"])
                if m["up"] != "-":
                    rec["upstream_duration_ms"] = int(m["up"])
                if m["reqid"] not in ("", "-"):
                    rec["x-request-id"] = m["reqid"]
                if m["upstream"] not in ("", "-"):
                    rec["upstream_host"] = m["upstream"]
                yield i, rec
