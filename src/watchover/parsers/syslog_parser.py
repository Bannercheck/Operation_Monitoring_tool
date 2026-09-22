"""Syslog: BSD (RFC 3164) 'Sep 16 14:31:02 host program[pid]: message', RFC 5424 '<PRI>1 ts host app procid msgid [sd] msg',
Cisco ASA '%ASA-4-106023:' ids, Palo Alto PAN-OS CSV payloads, and lines whose header has no program."""

from __future__ import annotations

import re
from typing import Iterator

from ..format_detector import RFC5424_RE, SYSLOG_NOPROG_RE, SYSLOG_RE
from .base import Parser

ASA_RE = re.compile(r"^%(?P<vendor>ASA|FTD|PIX|FWSM)-(?P<sev>\d)-(?P<id>\d+)$")
SD_RE = re.compile(r'\[([\w@.\-]+)((?:\s+[\w.\-]+="(?:[^"\\]|\\.)*")*)\]')
SD_KV_RE = re.compile(r'([\w.\-]+)="((?:[^"\\]|\\.)*)"')
PAN_TYPES = {"TRAFFIC": "INFO", "THREAT": "WARN", "SYSTEM": "INFO", "CONFIG": "INFO", "USERID": "INFO", "HIPMATCH": "INFO", "GLOBALPROTECT": "INFO"}
PAN_SEV = {"critical": "CRITICAL", "high": "ERROR", "medium": "WARN", "low": "INFO", "informational": "INFO"}


def _pan(msg: str) -> dict | None:
    """PAN-OS syslog payload: '1,receive time,serial,TYPE,subtype,...' (CSV). Enough of the layout to classify the record."""
    if not msg.startswith("1,") or msg.count(",") < 20:
        return None
    f = msg.split(",")
    typ, sub = f[3].strip(), f[4].strip()
    rec = {"service": "pan-os", "pan_type": typ, "pan_subtype": sub, "message": f"{typ} {sub} {f[7]} -> {f[8]}".strip()}
    if typ == "THREAT":
        sev = next((PAN_SEV[x.strip().lower()] for x in f[30:40] if x.strip().lower() in PAN_SEV), "WARN")
        rec["severity"] = sev
        threat = next((x.strip('"') for x in f[30:36] if x.startswith('"')), "")
        rec["message"] = f"{typ} {sub} {f[7]} -> {f[8]} {threat}".strip()
    else:
        rec["severity"] = PAN_TYPES.get(typ, "INFO")
    if len(f) > 1 and f[1].strip():
        rec["timestamp"] = f[1].strip()
    return rec


class SyslogParser(Parser):
    name = "syslog"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            if m := RFC5424_RE.match(ln):
                rec = {"timestamp": m["ts"], "host": m["host"], "program": m["app"] if m["app"] != "-" else "", "message": m["msg"]}
                if m["pid"] and m["pid"] != "-":
                    rec["pid"] = m["pid"]
                if m["msgid"] and m["msgid"] != "-":
                    rec["msgid"] = m["msgid"]
                rec["severity"] = str(int(m["pri"]) % 8)
                for sd in SD_RE.finditer(m["sd"] or ""):
                    rec.update({f"{sd[1]}.{k}": v for k, v in SD_KV_RE.findall(sd[2])})
                if rec["message"].startswith("﻿"):
                    rec["message"] = rec["message"][1:]
                yield i, rec
                continue
            m = SYSLOG_RE.match(ln)
            if m:
                rec = {"timestamp": m["ts"], "host": m["host"], "program": m["prog"], "message": m["msg"]}
                if m["pid"]:
                    rec["pid"] = m["pid"]
                if m["pri"]:
                    rec["severity"] = str(int(m["pri"]) % 8)
                if a := ASA_RE.match(m["prog"]):                               # Cisco: the "program" is the message id, its digit is the severity
                    rec.update({"program": a["vendor"].lower(), "severity": a["sev"], "msgid": a["id"]})
                yield i, rec
                continue
            m = SYSLOG_NOPROG_RE.match(ln)
            if m:
                rec = {"timestamp": m["ts"], "host": m["host"], "message": m["msg"]}
                if m["pri"]:
                    rec["severity"] = str(int(m["pri"]) % 8)
                if pan := _pan(m["msg"]):
                    rec.update(pan)
                yield i, rec
