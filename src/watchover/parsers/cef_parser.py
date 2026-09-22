"""ArcSight CEF and QRadar LEEF (security devices: firewalls, EDR, SIEM forwarders), optionally behind a syslog header."""

from __future__ import annotations

import re
from typing import Iterator

from .base import Parser

CEF_HEAD = re.compile(r"^(?P<prefix>.*?)CEF:(?P<ver>\d+)\|")
LEEF_HEAD = re.compile(r"^(?P<prefix>.*?)LEEF:(?P<ver>\d+(?:\.\d+)?)\|")
SYSLOG_PREFIX = re.compile(r"^(?:<\d+>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+(?P<host>\S+)\s*$")
CEF_KV = re.compile(r"(\w+)=((?:\\.|[^\\=])*?)(?=\s+\w+=|$)")
CEF_SEV = lambda v: "CRITICAL" if v >= 9 else "ERROR" if v >= 7 else "WARN" if v >= 4 else "INFO"  # noqa: E731


def _split_header(s: str, n: int) -> list[str]:
    """n pipe-separated fields where '\\|' is an escaped pipe; the rest of the string is the last element."""
    out, cur, i = [], [], 0
    while i < len(s) and len(out) < n:
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i + 1]); i += 2; continue
        if c == "|":
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    out.append(s[i:] if len(out) == n else "".join(cur))
    return out


class CefParser(Parser):
    name = "cef"
    kind = "event"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            rec: dict = {}
            if m := CEF_HEAD.match(ln):
                vendor, product, version, sig, name, sev, ext = _split_header(ln[m.end():], 6)
                rec = {"vendor": vendor, "product": product, "version": version, "signature": sig, "message": name, "service": product.strip() or vendor}
                if sev.strip().isdigit():
                    rec["severity"] = CEF_SEV(int(sev))
                elif sev.strip():
                    rec["severity"] = sev.strip()
                pairs = {k: v.replace("\\=", "=").replace("\\n", " ").strip() for k, v in CEF_KV.findall(ext)}
            elif m := LEEF_HEAD.match(ln):
                vendor, product, version, name, rest = _split_header(ln[m.end():], 4)
                rec = {"vendor": vendor, "product": product, "version": version, "message": name, "service": product.strip() or vendor}
                delim = "\t"
                if rest.startswith("|") is False and len(rest) > 1 and rest[0] not in "\t" and "|" in rest[:8] and rest[0] != " ":
                    delim, rest = rest.split("|", 1)[0], rest.split("|", 1)[1]       # LEEF 2.0: custom delimiter field, e.g. "^"
                elif rest.startswith("|"):
                    rest = rest[1:]
                pairs = {}
                for part in rest.split(delim):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        pairs[k.strip()] = v.strip()
                if "sev" in pairs and pairs["sev"].isdigit():
                    rec["severity"] = CEF_SEV(int(pairs["sev"]))
            else:
                continue
            if m["prefix"] and (sp := SYSLOG_PREFIX.match(m["prefix"].strip())):
                rec.setdefault("host", sp["host"])
                rec.setdefault("syslog_ts", sp["ts"])
            ts = pairs.get("rt") or pairs.get("devTime") or pairs.get("end") or pairs.get("start") or rec.get("syslog_ts")
            if ts:
                rec["timestamp"] = ts
            host = pairs.get("dvchost") or pairs.get("identHostName") or pairs.get("dvc") or pairs.get("devname") or rec.get("host")
            if host:
                rec["host"] = host
            if pairs.get("msg"):
                rec["message"] = f"{rec['message']}: {pairs['msg']}"
            rec.update({k: v for k, v in pairs.items() if k not in rec})
            yield i, rec
