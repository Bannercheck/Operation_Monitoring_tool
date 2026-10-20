"""SAP PO / PI integrations: every message the Adapter Engine handled, from its read-only monitoring API, as Watchover data.

The source (Ayarlar › Veri kaynakları, kind "sap-po") polls the Adapter Engine's standard web service
`AdapterMessageMonitoringVi.getMessageList` (POST <PO>/AdapterMessageMonitoring/basic?style=document) for the messages of the last
interval: header data only, never a payload (KVKK). The technical user needs read-only message monitoring in PO (Basis assigns the
display role for the Adapter Engine message monitor; no change rights). Each message becomes:

  * one log line on the PO host: "SI_Order_Out  BS_ECC_PRD → BS_CRM  delivered  230 ms" with po.* fields to search and facet on;
  * a trace whose id is the PO message id: sender system (producer) → SAP PO (the Integration / Adapter Engine, its processing
    time and status) → receiver system (consumer), so APM, the service map and the trace waterfall show the integration;
  * two integration-map edges: sender system → PO host and PO host → receiver system, labelled with the adapter types
    (SOAP, IDoc, RFC, REST, JDBC, File / SFTP, JMS, Mail ...) and carrying the SAP PO technology tag.

Business-system names (BS_ECC_PRD) appear on the map as they are; an inventory alias maps one to its real server.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
SOAP_PATH = "/AdapterMessageMonitoring/basic?style=document"

# Adapter Engine status -> (word shown, severity, error)
STATUS = {
    "success": ("delivered", "INFO", False), "delivered": ("delivered", "INFO", False), "dlvd": ("delivered", "INFO", False),
    "delivering": ("delivering", "INFO", False), "tobedelivered": ("waiting", "WARN", False), "waiting": ("waiting", "WARN", False),
    "holding": ("holding", "WARN", False), "hold": ("holding", "WARN", False), "scheduled": ("waiting", "WARN", False),
    "systemerror": ("error", "ERROR", True), "ndlv": ("error", "ERROR", True), "fail": ("error", "ERROR", True), "error": ("error", "ERROR", True),
    "canceled": ("canceled", "WARN", True), "cancelled": ("canceled", "WARN", True),
}
# adapter type -> (map label, a typical port the map uses to name the protocol; 0: no telling port, the map says "jdbc")
ADAPTERS = {
    "soap": ("SOAP", 443), "xi": ("XI", 50001), "rest": ("REST", 443), "http": ("HTTP", 443), "http_aae": ("HTTP", 443),
    "idoc": ("IDoc", 3300), "idoc_aae": ("IDoc", 3300), "rfc": ("RFC", 3300), "jdbc": ("JDBC", 0), "file": ("File", 22),
    "ftp": ("FTP", 21), "sftp": ("SFTP", 22), "jms": ("JMS", 5672), "mail": ("Mail", 25), "odata": ("OData", 443), "as2": ("AS2", 443),
}
GUIDISH = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def po_host(url: str) -> str:
    """The PO server as agents name it: the URL's host, short name (sappo01.sirket.com.tr -> sappo01; an IP stays as it is)."""
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or "sap-po").lower()
    return h if re.match(r"^\d+(\.\d+){3}$", h) or ":" in h else h.split(".")[0]


def request_xml(since: datetime, until: datetime, limit: int = 1000) -> bytes:
    """The getMessageList request for [since, until] (Adapter Engine local time is taken from the offsets)."""
    f = lambda t: t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")   # noqa: E731
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:AdapterMessageMonitoringVi"
  xmlns:urn1="urn:com.sap.aii.mdt.server.adapterframework.ws" xmlns:urn2="urn:com.sap.aii.mdt.api.data">
 <soapenv:Header/>
 <soapenv:Body>
  <urn:getMessageList>
   <urn:filter>
    <urn1:archive>false</urn1:archive>
    <urn1:dateType>0</urn1:dateType>
    <urn1:fromTime>{f(since)}</urn1:fromTime>
    <urn1:nodeId>0</urn1:nodeId>
    <urn1:onlyFaultyMessages>false</urn1:onlyFaultyMessages>
    <urn1:retries>0</urn1:retries>
    <urn1:retryInterval>0</urn1:retryInterval>
    <urn1:timesFailed>0</urn1:timesFailed>
    <urn1:toTime>{f(until)}</urn1:toTime>
    <urn1:wasEdited>false</urn1:wasEdited>
   </urn:filter>
   <urn:maxMessages>{int(limit)}</urn:maxMessages>
  </urn:getMessageList>
 </soapenv:Body>
</soapenv:Envelope>""".encode()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(el, *names) -> str:
    """The text of the first direct or nested child whose local name is one of `names` (namespace-agnostic)."""
    want = {n.lower() for n in names}
    for c in el.iter():
        if c is not el and _local(c.tag) in want and (c.text or "").strip():
            return c.text.strip()
    return ""


def _ts(v: str) -> datetime | None:
    if not v:
        return None
    v = v.strip().replace(" ", "T")
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2}|Z)?", v)
        if not m:
            return None
        tz = (m.group(3) or "+00:00").replace("Z", "+00:00")
        tz = tz if ":" in tz else tz[:3] + ":" + tz[3:]
        try:
            d = datetime.fromisoformat(m.group(1) + tz)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _adapter(v: str) -> str:
    """'SOAP_http://sap.com/xi/XI/System' / 'IDoc_AAE' / 'File' -> map label."""
    k = re.split(r"[_ :/]", (v or "").strip(), maxsplit=1)[0].lower()
    if (v or "").lower().startswith(("idoc_aae", "http_aae")):
        k = v.lower()[:8]
    return ADAPTERS.get(k, (v.split("_")[0][:20] if v else "", 443))[0]


def parse_messages(xml: bytes) -> list[dict]:
    """getMessageList answer -> message headers. Namespace-agnostic: any element holding a messageID child is a message.
    ElementTree never resolves external entities (no XXE); a DOCTYPE is refused outright."""
    if b"<!DOCTYPE" in xml[:2048].upper():
        raise ValueError("unexpected DOCTYPE in the PO answer")
    root = ET.fromstring(xml)
    fault = next((e for e in root.iter() if _local(e.tag) == "fault"), None)
    if fault is not None:
        raise ValueError("PO answered a SOAP fault: " + (_text(fault, "faultstring", "text") or "unknown")[:200])
    out, seen = [], set()
    for el in root.iter():
        kids = {_local(c.tag) for c in list(el)}
        if "messageid" not in kids:
            continue
        mid = _text(el, "messageID")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        iface = next((c for c in list(el) if _local(c.tag) == "interface"), None)
        st_raw = _text(el, "status")
        start, end = _ts(_text(el, "startTime")), _ts(_text(el, "endTime"))
        out.append({
            "id": mid.lower(), "key": _text(el, "messageKey"), "status_raw": st_raw,
            "start": start, "end": end,
            "sender": _text(el, "senderName"), "sender_party": _text(el, "senderParty"),
            "receiver": _text(el, "receiverName"), "receiver_party": _text(el, "receiverParty"),
            "interface": (_text(iface, "name") if iface is not None else "") or _text(el, "senderInterface", "interfaceName"),
            "namespace": (_text(iface, "namespace") if iface is not None else "") or _text(el, "senderInterfaceNamespace"),
            "adapter": _adapter(_text(el, "connectionName", "protocol", "adapterType")),
            "direction": _text(el, "direction").upper(), "qos": _text(el, "qualityOfService"),
            "error": (_text(el, "errorLabel") or _text(el, "errorCategory") or _text(el, "errorCode"))[:200],
            "ref": _text(el, "refToMessageId").lower(), "conversation": _text(el, "conversationID", "conversationId"),
            "node": _text(el, "nodeId"),
        })
    return out


def status_of(m: dict) -> tuple[str, str, bool]:
    return STATUS.get(re.sub(r"[^a-z]", "", (m.get("status_raw") or "").lower()), ((m.get("status_raw") or "unknown").lower()[:20], "INFO", False))


def duration_ms(m: dict) -> float | None:
    if m.get("start") and m.get("end"):
        d = (m["end"] - m["start"]).total_seconds() * 1000
        return round(d, 1) if 0 <= d <= 24 * 3600 * 1000 else None
    return None


def _sid(*parts) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def rows_of(msgs: list[dict], po_host: str) -> list[dict]:
    """One log row per message for the live feed (Sources poller -> JSONL). Ids live under po.* so the generic span builder does
    not make a second trace from the line (feed() writes the trace)."""
    rows = []
    for m in msgs:
        word, lvl, err = status_of(m)
        dur = duration_ms(m)
        snd, rcv = m["sender"] or m["sender_party"] or "?", m["receiver"] or m["receiver_party"] or "?"
        took = f"  {dur / 1000:.2f} s" if dur is not None else ""     # seconds: "230 ms" in the text would make the line a span of its own
        msg = f"{m['interface'] or '-'}  {snd} → {rcv}  {word}{took}" + (f"  {m['error']}" if m.get("error") else "")
        rows.append({k: v for k, v in {
            "ts": (m["start"] or datetime.now(UTC)).isoformat(), "level": lvl, "host": po_host, "service": "sap-po", "msg": msg,
            "po.msg_id": m["id"], "po.status": word, "po.sender": snd, "po.sender_party": m["sender_party"], "po.receiver": rcv,
            "po.receiver_party": m["receiver_party"], "po.interface": m["interface"], "po.namespace": m["namespace"], "po.adapter": m["adapter"],
            "po.direction": m["direction"], "po.qos": m["qos"], "po.error": m["error"], "po.ref_id": m["ref"], "po.duration_ms": dur,
            "session_id": m["conversation"] or m["ref"] or m["id"],          # a request / response pair (or a conversation) is one session
        }.items() if v not in (None, "")})
    return rows


def spans_of(msgs: list[dict], po_host: str, env: str = "") -> list[dict]:
    """Three spans per message: sender (producer) -> SAP PO (internal: the engine's processing time) -> receiver (consumer).
    The explicit parent ids keep the order (the stages do not nest in time); the trace id is the PO message id, so the
    referenced message of a synchronous answer (refToMessageId) shares the request's trace."""
    out = []
    for m in msgs:
        word, lvl, err = status_of(m)
        dur = duration_ms(m)
        start = m["start"] or datetime.now(UTC)
        trace = (m["ref"] if m["ref"] and GUIDISH.match(m["ref"]) else m["id"]).replace("-", "")
        snd, rcv = m["sender"] or m["sender_party"] or "?", m["receiver"] or m["receiver_party"] or "?"
        op = (m["interface"] or "-")[:160]
        s1, s2, s3 = _sid(m["id"], "s"), _sid(m["id"], "po"), _sid(m["id"], "r")
        sess = (m["conversation"] or m["ref"] or m["id"])[:128]
        base = {"trace_id": trace, "env": env, "user_ref": None, "session_id": sess, "link": "explicit", "confidence": 1.0,
                "ref": f"sap-po:{m['id']}", "status": 0, "attrs": {"po.msg_id": m["id"], "po.adapter": m["adapter"], "po.status": word}}
        msg = f"{op} {snd} → {rcv} {word}"
        out.append({**base, "span_id": s1, "parent_id": None, "start": start, "duration_ms": dur or 0.0, "has_duration": dur is not None,
                    "service": snd[:120], "operation": f"send {op}"[:160], "kind": "producer", "error": 0, "host": snd[:120],
                    "message": msg[:300], "severity": "INFO"})
        out.append({**base, "span_id": s2, "parent_id": s1, "start": start, "duration_ms": dur or 0.0, "has_duration": dur is not None,
                    "service": "SAP PO", "operation": op, "kind": "internal", "error": int(err), "host": po_host[:120],
                    "message": (msg + (f" {m['error']}" if m.get("error") else ""))[:300], "severity": lvl})
        out.append({**base, "span_id": s3, "parent_id": s2, "start": (m["end"] or start), "duration_ms": 0.0, "has_duration": False,
                    "service": rcv[:120], "operation": f"receive {op}"[:160], "kind": "consumer", "error": int(err), "host": rcv[:120],
                    "message": msg[:300], "severity": lvl})
    return out


def flows_of(msgs: list[dict], po_host: str, env: str = "") -> list:
    """Integration-map edges, one per (sender, adapter) into PO and (receiver, adapter) out of PO, with the message count."""
    from .models import Observation
    inbound, outbound = Counter(), Counter()
    for m in msgs:
        snd, rcv, ad = m["sender"] or m["sender_party"], m["receiver"] or m["receiver_party"], m["adapter"] or "PO"
        if snd:
            inbound[(snd, ad)] += 1
        if rcv:
            outbound[(rcv, ad)] += 1
    port = lambda ad: next((p for lbl, p in ADAPTERS.values() if lbl == ad), 443)   # noqa: E731
    now = datetime.now(UTC)
    obs = []
    for (snd, ad), n in inbound.items():                     # sender system -> PO (the sender's side of the edge)
        obs.append(Observation(timestamp=now, message=f"flow out {ad} {po_host}", host=snd[:120], environment=env, kind="event",
                               attributes={"flow.dir": "out", "flow.remote": f"{po_host}:{port(ad)}", "flow.process": f"PO {ad}"[:60],
                                           "flow.count": n, "flow.tech": "sap-po"}))
    for (rcv, ad), n in outbound.items():                    # PO -> receiver system
        obs.append(Observation(timestamp=now, message=f"flow out {ad} {rcv}", host=po_host[:120], environment=env, kind="event",
                               attributes={"flow.dir": "out", "flow.remote": f"{rcv}:{port(ad)}", "flow.process": f"PO {ad}"[:60],
                                           "flow.count": n, "flow.tech": "sap-po"}))
    return obs


def messages_from_rows(rows: list[dict]) -> list[dict]:
    """The poller hands the kept (not yet seen) rows to feed(): rebuild the message headers from their po.* fields."""
    out = []
    for r in rows:
        if not r.get("po.msg_id"):
            continue
        start = _ts(r.get("ts", ""))
        dur = r.get("po.duration_ms")
        out.append({"id": r["po.msg_id"], "status_raw": r.get("po.status", ""), "start": start,
                    "end": start + timedelta(milliseconds=float(dur)) if start and isinstance(dur, (int, float)) else None,
                    "sender": r.get("po.sender", ""), "sender_party": r.get("po.sender_party", ""), "receiver": r.get("po.receiver", ""),
                    "receiver_party": r.get("po.receiver_party", ""), "interface": r.get("po.interface", ""), "namespace": r.get("po.namespace", ""),
                    "adapter": r.get("po.adapter", ""), "direction": r.get("po.direction", ""), "qos": r.get("po.qos", ""), "error": r.get("po.error", ""),
                    "ref": r.get("po.ref_id", ""), "conversation": "" if r.get("session_id") in (r.get("po.ref_id"), r.get("po.msg_id")) else r.get("session_id", ""),
                    "key": "", "node": ""})
    return out


def feed(rows: list[dict], po_host: str, env: str, traces=None, flows=None) -> dict:
    """After the poller ingested the rows as log lines: their traces and integration-map edges."""
    msgs = messages_from_rows(rows)
    n_sp = traces.add_spans(spans_of(msgs, po_host, env), keep_all=True) if traces is not None and msgs else 0
    n_fl = flows.add(flows_of(msgs, po_host, env)) if flows is not None and msgs else 0
    return {"messages": len(msgs), "spans": n_sp, "flows": n_fl}


# ---------------------------------------------------------------- PO's own Java log (defaultTrace, #2.0# lines from the agent)
_PO_LOGGER = re.compile(r"com\.sap\.(?:aii|xi)\b|\bXI\b|messaging|adapter", re.I)
_MSG_ID = re.compile(r"(?i)\b(?:message|msg)(?:[ _-]?id)?\s*[:=#]?\s*['\"(]?([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})\b")
_CHANNEL = re.compile(r"\b(CC_[A-Za-z0-9_\-]{2,78})\b")


def tag_log(o) -> None:
    """A NetWeaver Java line of the PO stack (logger / category com.sap.aii.*, com.sap.xi.*, messaging, adapter): the PO message id
    it names becomes trace_id (the same id the sap-po source gives that message's trace) and po.msg_id; a CC_* name po.channel."""
    a = o.attributes
    if a.get("sap.format") != "nw-java" or "trace_id" in a:
        return
    where = " ".join(str(a.get(k) or "") for k in ("sap.logger", "sap.category", "sap.application")) + " " + (o.origin or "")
    if not _PO_LOGGER.search(where):
        return
    msg = o.message or ""
    m = _MSG_ID.search(msg)
    if m:
        mid = m.group(1).lower()
        a["po.msg_id"], a["trace_id"] = mid, mid.replace("-", "")
    c = _CHANNEL.search(msg)
    if c:
        a["po.channel"] = c.group(1)
