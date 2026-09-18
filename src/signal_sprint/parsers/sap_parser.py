"""SAP log family: ABAP kernel developer traces (dev_w0, dev_disp, dev_rfc, dev_ms ...), SM21 system-log exports,
HANA traces (indexserver / nameserver *.trc), NetWeaver Java defaultTrace / applications.log (#2.0# list format),
Java util logging two-line format (std_server0.out, jvm_bootstrap.out), JVM GC logs (unified and classic), and
tp / R3trans / SUM / transport logs (`4 ETW000 ...`).

One stateful line parser: a line either opens a record, continues the previous one, or only moves the "current time"
forward (dev traces put the timestamp on its own line and the following lines inherit it). Any extension is accepted
(.log .lst .jvm .out .trc .file or none); the format is recognised from the content, never from the name.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

from .base import Parser

MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
DAYS = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

# dev_w0 style: "M Wed Sep 16 02:14:07:123 2026"  or  "A  Tue Sep 16 02:14:07 2026"
DEV_TS_RE = re.compile(rf"^([A-Z*])\s{{1,3}}{DAYS}\s+({MON})\s+(\d{{1,2}})\s+(\d{{2}}):(\d{{2}}):(\d{{2}})(?::(\d{{3}}))?\s+(\d{{4}})\s*$")
DEV_LINE_RE = re.compile(r"^([A-Z*])\s{1,3}(\S.*)$")
DEV_LEVEL_RE = re.compile(r"^\*\*\*\s*(ERROR|WARNING|LOG)\s*(?:([A-Z0-9]{3}))?\s*=>\s*(.*)$")
THR_RE = re.compile(r"^\[Thr\s+(\d+)\]\s*")
TRC_HEAD_RE = re.compile(r'^trc file:\s*"([^"]+)",\s*trc level:\s*(\d+),\s*release:\s*"([^"]+)"')
DEV_META_RE = re.compile(r"^(sid|sysno|pid|node\s*name|hostname|profile|relno|patchno)\s+(.+)$", re.I)

# HANA: "[12345]{-1}[-1/-1] 2026-09-16 02:14:07.123456 e Basis  TrexNet.cpp(00123) : message"
HANA_RE = re.compile(r"^\[(\d+)\]\{(-?\d+)\}\[(-?\d+)/(-?\d+)\]\s+(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+([iwefd])\s+(\S+)\s+(\S+?)\((\d+)\)\s*:\s*(.*)$")
HANA_LEVEL = {"i": "INFO", "w": "WARN", "e": "ERROR", "f": "CRITICAL", "d": "DEBUG"}

# NetWeaver Java list format: "#2.0 #2026 09 16 02:14:07:123#+0300#Error#com.sap.engine...#..."
NWJ_START_RE = re.compile(r"^#(\d\.\d)\s?#(\d{4})\s(\d{2})\s(\d{2})\s(\d{2}):(\d{2}):(\d{2})(?::(\d{3}))?#([+\-]\d{4}|[^#]*)#([^#]*)#([^#]*)#")
NWJ_LEVEL = {"error": "ERROR", "warning": "WARN", "info": "INFO", "debug": "DEBUG", "path": "DEBUG", "fatal": "CRITICAL", "plain": "INFO"}

# java.util.logging two-line: "Sep 16, 2026 2:14:07 AM com.sap.engine.core.Framework start" / "SEVERE: message"
JUL_HEAD_RE = re.compile(rf"^({MON})\s(\d{{1,2}}),\s(\d{{4}})\s(\d{{1,2}}):(\d{{2}}):(\d{{2}})\s(AM|PM)\s+(\S+)(?:\s+(\S+))?\s*$")
JUL_LINE_RE = re.compile(r"^(SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST):\s?(.*)$")
JUL_LEVEL = {"severe": "ERROR", "warning": "WARN", "info": "INFO", "config": "DEBUG", "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG"}

# JVM GC: unified "[2026-09-16T02:14:07.123+0300][12.345s][info][gc] GC(3) Pause Young ... 12.345ms"
GC_UNIFIED_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{4}|Z)?)\]((?:\[[^\]]*\])*)\s*(.*)$")
# classic "2026-09-16T02:14:07.123+0300: 12.345: [Full GC (Allocation Failure) ...]"
GC_CLASSIC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{4}|Z)?):\s+[\d.]+:\s+\[(.*)$")
GC_PAUSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms\b")

# tp / R3trans / SUM: "4 ETW000  date&time   : 16.09.2026 - 02:14:07" / "1 ETP111 exit code : "8"" / "2EETW125 ..."
TP_RE = re.compile(r"^(\d)\s?([A-Z])?([A-Z]{3}\d{3})[X ]?\s?(.*)$")
TP_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2}):(\d{2}):(\d{2})")
TP_STAMP_RE = re.compile(r"\b(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\b")
ALOG_RE = re.compile(r"^(START|STOP|ERR|WRN)\s+(\S+)\s+(\S+)?\s*(\S+)\s+(\d{4})\s+(\d{14})\s+(\S+)?\s*(\S+)?")

# SM21 export: "02:14:07 DIA  001 100 USER1  SE38  R6 8 Database error -1 at OPC access to table X" ; date lines "16.09.2026"
SM21_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(DIA|UPD|UP2|BTC|ENQ|SPO|RD|MS|DP|GW|IC|\w{2,3})\s+(\d{3})?\s*(\d{3})?\s*(\S*)\s+(\S*)\s+([A-Z][A-Z0-9])\s+(\d)\s+(.*)$")
DATE_ONLY_RE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")
SM21_TYPE_LEVEL = {"0": "INFO", "1": "INFO", "2": "INFO", "3": "WARN", "4": "WARN", "5": "ERROR", "6": "ERROR", "7": "ERROR", "8": "CRITICAL", "9": "CRITICAL"}
ERR_WORDS = re.compile(r"\b(error|failed|failure|abort|dump|exception|rc\s*=\s*(?:8|12|16)|exit code\s*:\s*\"?(?:8|12|16)|cannot|unable|not found|denied|timeout|shortdump)\b", re.I)
WARN_WORDS = re.compile(r"\b(warning|retry|deprecated|slow|rc\s*=\s*4|exit code\s*:\s*\"?4)\b", re.I)

FAMILY_MARKERS = [DEV_TS_RE, TRC_HEAD_RE, HANA_RE, NWJ_START_RE, JUL_HEAD_RE, GC_UNIFIED_RE, GC_CLASSIC_RE, TP_RE, ALOG_RE, SM21_RE]


def _iso(y, mo, d, h, mi, s, ms=None) -> str:
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(h):02d}:{int(mi):02d}:{int(s):02d}" + (f".{int(ms):03d}" if ms else "")


def _sev_from_words(text: str, default: str = "INFO") -> str:
    if ERR_WORDS.search(text):
        return "ERROR"
    if WARN_WORDS.search(text):
        return "WARN"
    return default


def sap_score(lines: list[str]) -> float:
    """Share of non-empty lines that look like one of the SAP families (used by format_detector)."""
    if not lines:
        return 0.0
    hits = 0
    for ln in lines:
        if any(r.match(ln) for r in FAMILY_MARKERS) or JUL_LINE_RE.match(ln) or ln.startswith("#"):
            hits += 1
        elif DEV_LINE_RE.match(ln) and (ln.startswith(("M ", "A ", "B ", "C ", "N ", "S ", "X ", "Y ", "I ", "G ", "E ", "* ", "T ")) or "***" in ln):
            hits += 0.6
    return round(hits / len(lines), 2)


class SapParser(Parser):
    name = "sap"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        cur_ts: str | None = None           # "current time" carried across lines (dev traces, tp logs, SM21)
        cur_date: tuple | None = None
        meta: dict = {}                     # sid / pid / host from dev trace headers
        pending_jul: tuple | None = None    # (line_no, ts, logger, method)
        nwj: tuple | None = None            # (line_no, rec) being accumulated (multi-line # records)
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.rstrip()
            if not s.strip():
                continue
            # ---- NetWeaver Java list format (multi-line records) ----
            if m := NWJ_START_RE.match(s):
                if nwj:
                    yield nwj[0], self._finish_nwj(nwj[1])
                ts = _iso(m[2], m[3], m[4], m[5], m[6], m[7], m[8])
                tz = m[9] if re.fullmatch(r"[+\-]\d{4}", m[9] or "") else ""
                rec = {"timestamp": ts + (f"{tz[:3]}:{tz[3:]}" if tz else ""), "severity": NWJ_LEVEL.get((m[10] or "").strip().lower(), "INFO"),
                       "origin": (m[11] or "").strip(), "sap.format": "nw-java", "_body": [s[m.end():]]}
                if s.endswith("#") and s.count("#") >= 8:       # single-line record
                    yield i, self._finish_nwj(rec)
                else:
                    nwj = (i, rec)
                continue
            if nwj:                                              # continuation of a multi-line record
                nwj[1]["_body"].append(s)
                if s.endswith("#"):
                    yield nwj[0], self._finish_nwj(nwj[1])
                    nwj = None
                continue
            # ---- HANA trace ----
            if m := HANA_RE.match(s):
                yield i, {"timestamp": m[5].replace(" ", "T"), "severity": HANA_LEVEL.get(m[6], "INFO"), "service": "hana-" + m[7].lower(),
                          "origin": m[7], "message": m[10].strip(), "sap.thread": m[1], "sap.connection": m[2], "sap.source": f"{m[8]}:{m[9]}", "sap.format": "hana"}
                continue
            # ---- JVM GC ----
            if m := GC_UNIFIED_RE.match(s):
                tags = re.findall(r"\[([^\]]*)\]", m[2])
                level = next((t for t in tags if t in ("info", "warning", "error", "debug", "trace")), "info")
                pause = GC_PAUSE_RE.search(m[3])
                sev = {"warning": "WARN", "error": "ERROR", "debug": "DEBUG", "trace": "DEBUG"}.get(level, "INFO")
                if pause and float(pause[1]) >= 1000:
                    sev = "ERROR" if float(pause[1]) >= 5000 else "WARN"
                elif "Full GC" in m[3] or "Pause Full" in m[3]:
                    sev = "WARN"
                rec = {"timestamp": m[1], "severity": sev, "service": "jvm-gc", "message": m[3].strip(), "sap.format": "jvm-gc", "gc.tags": ",".join(t for t in tags if t not in (level,) and not t.endswith("s"))}
                if pause:
                    rec["gc.pause_ms"] = pause[1]
                yield i, rec
                continue
            if m := GC_CLASSIC_RE.match(s):
                pause = re.search(r"(\d+(?:\.\d+)?)\s*secs", m[2])
                sev = "WARN" if "Full GC" in m[2] else "INFO"
                if pause and float(pause[1]) >= 1.0:
                    sev = "ERROR" if float(pause[1]) >= 5.0 else "WARN"
                rec = {"timestamp": m[1], "severity": sev, "service": "jvm-gc", "message": "[" + m[2].strip(), "sap.format": "jvm-gc"}
                if pause:
                    rec["gc.pause_ms"] = str(round(float(pause[1]) * 1000))
                yield i, rec
                continue
            # ---- java.util.logging two-line ----
            if m := JUL_HEAD_RE.match(s):
                h = int(m[4]) % 12 + (12 if m[7] == "PM" else 0)
                pending_jul = (i, _iso(m[3], MONTHS[m[1]], m[2], h, m[5], m[6]), m[8], m[9] or "")
                cur_ts = pending_jul[1]
                continue
            if pending_jul and (m := JUL_LINE_RE.match(s)):
                ln_no, ts, logger, method = pending_jul
                pending_jul = None
                yield ln_no, {"timestamp": ts, "severity": JUL_LEVEL.get(m[1].lower(), "INFO"), "origin": logger, "service": _svc_from_logger(logger),
                              "message": m[2].strip(), "sap.method": method, "sap.format": "java-util-logging"}
                continue
            # ---- ABAP kernel developer trace ----
            if m := TRC_HEAD_RE.match(s):
                meta.update({"sap.trc_file": m[1], "sap.trc_level": m[2], "sap.release": m[3]})
                continue
            if m := DEV_TS_RE.match(s):
                cur_ts = _iso(m[8], MONTHS[m[2]], m[3], m[4], m[5], m[6], m[7])
                continue
            if set(s.strip()) <= set("-=*_ "):                   # separator lines
                continue
            if m := DEV_LINE_RE.match(s):
                comp, body = m[1], m[2]
                if comp == "*" and not body.startswith("***"):     # "*  ACTIVE TRACE LEVEL 1" header decoration
                    continue
                if mm := DEV_META_RE.match(body):
                    key = mm[1].lower().replace(" ", "")
                    val = mm[2].strip()
                    if key == "sid":
                        meta["sap.sid"] = val
                    elif key == "pid":
                        meta["sap.pid"] = val
                    elif key in ("nodename", "hostname"):
                        meta["host"] = val
                    elif key == "profile":
                        meta.setdefault("host", val.rsplit("_", 1)[-1])
                        meta["sap.instance"] = val.rsplit("/", 1)[-1]
                    continue
                thr = ""
                if mt := THR_RE.match(body):
                    thr, body = mt[1], body[mt.end():]
                sev, code = "INFO", ""
                if ml := DEV_LEVEL_RE.match(body):
                    sev = {"ERROR": "ERROR", "WARNING": "WARN", "LOG": "INFO"}[ml[1]]
                    code, body = ml[2] or "", ml[3]
                    if ml[1] == "LOG":
                        sev = _sev_from_words(body, "INFO")
                elif body.startswith("***"):
                    sev = "ERROR"
                if not cur_ts and not body:
                    continue
                rec = {"timestamp": cur_ts, "severity": sev, "message": body.strip(), "sap.component": comp, "sap.format": "dev-trace",
                       "service": f"sap-{meta['sap.sid'].lower()}" if meta.get("sap.sid") else "sap-" + (meta.get("sap.trc_file", "kernel").split(".")[0].lower()), **{k: v for k, v in meta.items() if k != "host"}}
                if meta.get("host"):
                    rec["host"] = meta["host"]
                if thr:
                    rec["sap.thread"] = thr
                if code:
                    rec["sap.msg_code"] = code
                yield i, rec
                continue
            # ---- SM21 system log export ----
            if m := SM21_RE.match(s):
                ts = (f"{cur_date[2]}-{cur_date[1]}-{cur_date[0]}T{m[1]}" if cur_date else None)
                sev = SM21_TYPE_LEVEL.get(m[8], "INFO")
                yield i, {"timestamp": ts, "severity": sev, "service": f"sap-{m[2].lower()}", "message": m[9].strip(), "sap.wp_type": m[2], "sap.wp_no": m[3] or "",
                          "sap.client": m[4] or "", "sap.user": m[5], "sap.tcode": m[6], "sap.msg_code": m[7] + m[8], "sap.format": "sm21"}
                continue
            # ---- transport ALOG / SLOG ----
            if m := ALOG_RE.match(s):
                st = m[6]
                ts = _iso(st[:4], st[4:6], st[6:8], st[8:10], st[10:12], st[12:14])
                sev = {"ERR": "ERROR", "WRN": "WARN"}.get(m[1], "INFO")
                yield i, {"timestamp": ts, "severity": sev, "service": f"sap-{(m[4] or '').lower()}-transport", "message": s.strip(), "sap.step": m[2], "sap.rc": m[5], "sap.format": "transport"}
                continue
            # ---- tp / R3trans / SUM ----
            if m := TP_RE.match(s):
                body = m[4]
                if md := TP_DATE_RE.search(body):
                    cur_ts = _iso(md[3], md[2], md[1], md[4], md[5], md[6])
                elif ms_ := TP_STAMP_RE.search(body):
                    cur_ts = _iso(*ms_.groups())
                sev = "ERROR" if (m[2] or "") == "E" else "WARN" if (m[2] or "") == "W" else _sev_from_words(body)
                yield i, {"timestamp": cur_ts, "severity": sev, "service": "sap-" + m[3][:3].lower(), "message": body.strip(), "sap.msg_code": m[3], "sap.level": m[1], "sap.format": "tp"}
                continue
            # ---- date-only lines (SM21 exports, tp headers) ----
            if (md := DATE_ONLY_RE.search(s)) and len(s.strip()) <= 40:
                cur_date = (md[1], md[2], md[3])
                continue
            # ---- anything else: keep as a message with the carried time ----
            yield i, {"timestamp": cur_ts, "severity": _sev_from_words(s), "message": s.strip(), "sap.format": "other", **({"service": f"sap-{meta['sap.sid'].lower()}"} if meta.get("sap.sid") else {})}
        if nwj:
            yield nwj[0], self._finish_nwj(nwj[1])

    @staticmethod
    def _finish_nwj(rec: dict) -> dict:
        body = "\n".join(rec.pop("_body"))
        fields = [f.strip() for f in body.split("#")]
        non_empty = [f for f in fields if f]
        rec["message"] = non_empty[-1] if non_empty else body.strip()
        if non_empty and re.fullmatch(r"[A-Z]{2,3}(?:-[A-Z0-9]+)+", non_empty[0]):    # "BC-JAS-DPL" component category
            rec["sap.category"] = non_empty[0]
        if rec.get("origin"):
            rec["service"] = _svc_from_logger(rec["origin"])
        return rec


def _svc_from_logger(logger: str) -> str:
    """com.sap.engine.services.deploy.X -> sap-deploy ; com.acme.Foo -> acme"""
    parts = [p for p in logger.split(".") if p]
    if len(parts) >= 3 and parts[:2] == ["com", "sap"]:
        rest = [p for p in parts[2:] if p.lower() not in ("engine", "services", "service", "core", "impl", "server", "interfaces", "jee", "tc")]
        return "sap-" + (rest[0].lower() if rest else parts[-1].lower())
    if len(parts) >= 2:
        return parts[1].lower()
    return logger.lower()
