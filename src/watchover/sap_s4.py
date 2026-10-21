"""SAP S/4HANA (and ECC / NetWeaver ABAP): the application layer the logs do not show, read through the system's SOAP-RFC service.

The source (Ayarlar › Veri kaynakları, kind "sap-s4") calls RFC_READ_TABLE over HTTP (POST <ICM>/sap/bc/soap/rfc?sap-client=NNN;
the SICF service /sap/bc/soap/rfc must be active) with a technical user that may only read these tables (S_RFC for RFC_READ_TABLE,
S_TABU_NAM display on the tables below; no change rights). No SAP library on the Watchover server. Per poll:

  * TBTCO   background jobs (SM37): aborted jobs, and jobs running longer than `long_job_min`
  * SNAP_BEG short dumps (ST22): time, application server, user, client, the runtime error when it can be read from the header
  * EDIDC   IDocs (WE02): errors become log lines; every IDoc's partner becomes an integration-map edge (inbound partner -> S/4,
            S/4 -> outbound partner, "IDoc")
  * ARFCSSTATE tRFC calls stuck in SYSFAIL / CPICERR (SM58): a log line and an edge S/4 -> RFC destination

Each finding is a log line on the S/4 host (s4.* fields, sap.user for the Sessions screen); anomalies, alerts and the log
explorer then work on them like on any line. Times are the SAP system's local time (`tz`, default Europe/Istanbul).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
SOAP_RFC = "/sap/bc/soap/rfc"
NS = "urn:sap-com:document:sap:rfc:functions"
DELIM = "|"

# IDoc statuses (WE47): outbound / inbound errors, and the ones that mean "done"
IDOC_ERROR = {"02", "04", "05", "25", "26", "29", "37", "40", "51", "56", "60", "61", "63", "65", "66", "69"}
IDOC_OK = {"03", "12", "16", "41", "53", "18", "06", "08"}
JOB_STATUS = {"A": "aborted", "F": "finished", "R": "running", "Y": "ready", "P": "scheduled", "S": "released", "Z": "put active"}
DUMP_ID = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,6})\b")      # TIME_OUT, DBSQL_DUPLICATE_KEY_ERROR, MESSAGE_TYPE_X ...

QUERIES = {
    "jobs": ("TBTCO", ("JOBNAME", "JOBCOUNT", "SDLUNAME", "STATUS", "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME")),
    "dumps": ("SNAP_BEG", ("DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "FLIST")),
    "idocs": ("EDIDC", ("DOCNUM", "STATUS", "DIRECT", "MESTYP", "IDOCTP", "SNDPRN", "RCVPRN", "UPDDAT", "UPDTIM")),
    "trfc": ("ARFCSSTATE", ("ARFCDEST", "ARFCSTATE", "ARFCFNAM", "ARFCDATUM", "ARFCUZEIT", "ARFCMSG")),
}


def _opts(where: str) -> list[str]:
    """RFC_READ_TABLE OPTIONS: the WHERE clause in lines of at most 72 characters, split at spaces."""
    out, cur = [], ""
    for w in where.split(" "):
        if len(cur) + len(w) + 1 > 72:
            out.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_table_xml(table: str, fields: tuple, where: str, rowcount: int = 1000) -> bytes:
    f = "".join(f"<item><FIELDNAME>{x}</FIELDNAME></item>" for x in fields)
    o = "".join(f"<item><TEXT>{_esc(x)}</TEXT></item>" for x in _opts(where))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="{NS}">
 <soapenv:Header/>
 <soapenv:Body>
  <urn:RFC_READ_TABLE>
   <DELIMITER>{DELIM}</DELIMITER><NO_DATA></NO_DATA><QUERY_TABLE>{table}</QUERY_TABLE><ROWCOUNT>{int(rowcount)}</ROWCOUNT><ROWSKIPS>0</ROWSKIPS>
   <DATA></DATA><FIELDS>{f}</FIELDS><OPTIONS>{o}</OPTIONS>
  </urn:RFC_READ_TABLE>
 </soapenv:Body>
</soapenv:Envelope>""".encode()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_read_table(xml: bytes, fields: tuple) -> list[dict]:
    """RFC_READ_TABLE answer -> rows (DATA/item/WA split at the delimiter, named by FIELDS). A SOAP fault or an ABAP exception
    (TABLE_NOT_AVAILABLE, NOT_AUTHORIZED ...) raises with its text; a DOCTYPE is refused (no XXE)."""
    if b"<!DOCTYPE" in xml[:2048].upper():
        raise ValueError("unexpected DOCTYPE in the SAP answer")
    root = ET.fromstring(xml)
    for e in root.iter():
        if _local(e.tag).lower() == "fault":
            txt = " ".join((x.text or "").strip() for x in e.iter() if _local(x.tag).lower() in ("faultstring", "name", "message", "text") and (x.text or "").strip())
            raise ValueError("SAP answered a fault: " + (txt or "unknown")[:200])
    names = []
    for e in root.iter():
        if _local(e.tag) == "FIELDS":
            names = [(_t(i, "FIELDNAME")) for i in e if _local(i.tag) == "item"] or names
    names = [n for n in names if n] or list(fields)
    rows = []
    for e in root.iter():
        if _local(e.tag) != "DATA":
            continue
        for i in e:
            wa = _t(i, "WA")
            if wa is None:
                continue
            parts = [p.strip() for p in wa.split(DELIM)]
            rows.append({n: (parts[k] if k < len(parts) else "") for k, n in enumerate(names)})
    return rows


def _t(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c.text or ""
    return None


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or "Europe/Istanbul")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=3))


def sap_ts(d: str, t: str, tz) -> datetime | None:
    """'20261021' + '091402' in the system's local time -> aware datetime."""
    d, t = re.sub(r"\D", "", d or ""), re.sub(r"\D", "", t or "")
    if len(d) != 8 or d == "00000000":
        return None
    t = (t + "000000")[:6]
    try:
        return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), int(t[:2]), int(t[2:4]), int(t[4:6]), tzinfo=tz)
    except ValueError:
        return None


def where_for(kind: str, since: datetime, tz) -> str:
    """The WHERE clause for one query: rows from the day `since` falls on (local time) onwards; the poller drops repeats."""
    day = since.astimezone(tz).strftime("%Y%m%d")
    return {"jobs": f"( STATUS = 'A' OR STATUS = 'R' ) AND STRTDATE >= '{day}'",
            "dumps": f"DATUM >= '{day}'",
            "idocs": f"UPDDAT >= '{day}'",
            "trfc": f"( ARFCSTATE = 'SYSFAIL' OR ARFCSTATE = 'CPICERR' ) AND ARFCDATUM >= '{day}'"}[kind]


def dump_error(flist: str) -> str:
    """The runtime error named in a SNAP_BEG header, when one reads as such (TIME_OUT, MESSAGE_TYPE_X ...)."""
    text = re.sub(r"[A-Z]{2}\d{3,4}", " ", flist or "")    # SNAP's field list packs "<tag><length><value>" (FC010TIME_OUT ...): cut the tags
    for m in DUMP_ID.finditer(text):
        w = m.group(1)
        if len(w) >= 6 and not w.startswith(("FC", "AP")):
            return w
    return ""


def rows_of(kind: str, recs: list[dict], s4_host: str, tz, now: datetime | None = None, long_job_min: int = 60) -> list[dict]:
    """Findings as log rows for the live feed (s4.* fields; sap.user feeds the Sessions screen)."""
    now = now or datetime.now(UTC)
    out = []
    for r in recs:
        if kind == "jobs":
            st = r.get("STATUS", "")
            start, end = sap_ts(r.get("STRTDATE"), r.get("STRTTIME"), tz), sap_ts(r.get("ENDDATE"), r.get("ENDTIME"), tz)
            if st == "R":
                if not start or (now - start).total_seconds() < long_job_min * 60:
                    continue
                msg, lvl, ts = f"Job {r.get('JOBNAME')} running longer than {long_job_min} min (user {r.get('SDLUNAME')})", "WARN", start   # one line per run: the same text every poll
            elif st == "A":
                took = f" after {(end - start).total_seconds() / 60:.1f} min" if start and end else ""
                msg, lvl, ts = f"Job {r.get('JOBNAME')} aborted{took} (user {r.get('SDLUNAME')})", "ERROR", end or start
            else:
                continue
            row = {"ts": (ts or now).isoformat(), "level": lvl, "host": s4_host, "service": "s4-jobs", "msg": msg, "s4.kind": "job",
                   "s4.job": r.get("JOBNAME"), "s4.job_count": r.get("JOBCOUNT"), "s4.job_status": JOB_STATUS.get(st, st), "sap.user": r.get("SDLUNAME")}
        elif kind == "dumps":
            ts = sap_ts(r.get("DATUM"), r.get("UZEIT"), tz)
            err = dump_error(r.get("FLIST", ""))
            row = {"ts": (ts or now).isoformat(), "level": "ERROR", "host": s4_host, "service": "s4-dumps",
                   "msg": f"ABAP short dump{' ' + err if err else ''} on {r.get('AHOST')} (user {r.get('UNAME')}, client {r.get('MANDT')})",
                   "s4.kind": "dump", "s4.dump": err, "s4.app_server": r.get("AHOST"), "s4.client": r.get("MANDT"), "sap.user": r.get("UNAME")}
        elif kind == "idocs":
            st = r.get("STATUS", "")
            if st not in IDOC_ERROR:
                continue
            inbound = r.get("DIRECT") == "2"
            partner = r.get("SNDPRN") if inbound else r.get("RCVPRN")
            ts = sap_ts(r.get("UPDDAT"), r.get("UPDTIM"), tz)
            row = {"ts": (ts or now).isoformat(), "level": "ERROR", "host": s4_host, "service": "s4-idoc",
                   "msg": f"IDoc {r.get('DOCNUM')} {r.get('MESTYP')} {'from' if inbound else 'to'} {partner}: status {st}",
                   "s4.kind": "idoc", "s4.idoc": r.get("DOCNUM"), "s4.idoc_status": st, "s4.message_type": r.get("MESTYP"), "s4.idoc_type": r.get("IDOCTP"),
                   "s4.direction": "inbound" if inbound else "outbound", "s4.partner": partner}
        elif kind == "trfc":
            ts = sap_ts(r.get("ARFCDATUM"), r.get("ARFCUZEIT"), tz)
            row = {"ts": (ts or now).isoformat(), "level": "ERROR", "host": s4_host, "service": "s4-trfc",
                   "msg": f"tRFC to {r.get('ARFCDEST')} {r.get('ARFCSTATE')}: {r.get('ARFCFNAM')} {r.get('ARFCMSG', '')}".strip(),
                   "s4.kind": "trfc", "s4.destination": r.get("ARFCDEST"), "s4.trfc_state": r.get("ARFCSTATE"), "s4.function": r.get("ARFCFNAM")}
        else:
            continue
        out.append({k: v for k, v in row.items() if v not in (None, "")})
    return out


def flow_rows(kind: str, recs: list[dict], s4_host: str) -> list[dict]:
    """Integration-map edges as rows with flow.* fields (the live store sends them to the flow table, not the log): IDoc
    partners (inbound partner -> S/4, S/4 -> outbound partner) and tRFC destinations, with counts."""
    if kind not in ("idocs", "trfc"):
        return []
    edges = Counter()
    for r in recs:
        if kind == "idocs":
            inbound = r.get("DIRECT") == "2"
            p = (r.get("SNDPRN") if inbound else r.get("RCVPRN")) or ""
            if p:
                edges[((p, s4_host) if inbound else (s4_host, p)), "IDoc"] += 1
        else:
            d = r.get("ARFCDEST") or ""
            if d:
                edges[(s4_host, d), "RFC"] += 1
    now = datetime.now(UTC).isoformat()
    return [{"ts": now, "host": src[:120], "msg": f"flow out S4 {ad} {dst}", "flow.dir": "out", "flow.remote": f"{dst}:3300",
             "flow.process": f"S4 {ad}", "flow.count": n, "flow.tech": "sap-abap"} for ((src, dst), ad), n in edges.items()]


REC_TIME = {"jobs": (("ENDDATE", "ENDTIME"), ("STRTDATE", "STRTTIME")), "dumps": (("DATUM", "UZEIT"),), "idocs": (("UPDDAT", "UPDTIM"),),
            "trfc": (("ARFCDATUM", "ARFCUZEIT"),)}
OVERLAP = timedelta(minutes=5)          # SAP clock vs ours; the poller's hash set drops the repeats inside it


def rec_time(kind: str, r: dict, tz) -> datetime | None:
    for d, t in REC_TIME[kind]:
        ts = sap_ts(r.get(d), r.get(t), tz)
        if ts:
            return ts
    return None


def fetch(call, s4_host: str, since: datetime, tz, long_job_min: int = 60, rowcount: int = 2000) -> list[dict]:
    """One poll: every query through `call(xml_bytes) -> answer bytes`; a query the user may not run (no authorisation, the table
    is missing on this release) is reported as one WARN row and the others still run; when none can be read (wrong password, the
    service is off) the poll fails with the first error, so the source shows it. The tables are read from the day of `since`
    (RFC_READ_TABLE filters by date only): records older than the poll window are dropped here, so a poll every 30 s does not
    re-send the day (log lines) nor re-count it (map edges count only records changed since the last poll). Running jobs are
    judged on every poll."""
    rows: list[dict] = []
    failed: list[str] = []
    for kind, (table, fields) in QUERIES.items():
        try:
            recs = parse_read_table(call(read_table_xml(table, fields, where_for(kind, since, tz), rowcount)), fields)
        except Exception as e:  # noqa: BLE001 - one table failing does not stop the others
            failed.append(f"{table}: {str(e)[:160]}")
            if len(failed) == len(QUERIES):                    # nothing could be read (wrong password, service off, wrong client): the source fails
                raise RuntimeError("no table could be read; " + failed[0]) from e
            hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)   # the same line within the hour: once an hour, not every poll
            rows.append({"ts": hour.isoformat(), "level": "WARN", "host": s4_host, "service": "s4-monitor",
                         "msg": f"{table} could not be read: {str(e)[:160]}", "s4.kind": "monitor", "s4.table": table})
            continue
        fresh = [r for r in recs if (kind == "jobs" and r.get("STATUS") == "R") or (rec_time(kind, r, tz) or since) >= since - OVERLAP]
        new = [r for r in recs if (rec_time(kind, r, tz) or since) >= since]
        rows += rows_of(kind, fresh, s4_host, tz, long_job_min=long_job_min) + flow_rows(kind, new, s4_host)
    return rows
