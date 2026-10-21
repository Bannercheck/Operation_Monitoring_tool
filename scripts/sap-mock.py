#!/usr/bin/env python3
"""SAP PO + S/4HANA stand-in for a test server without SAP: answers what Watchover's two SAP sources ask, with made-up traffic.

    python3 scripts/sap-mock.py                       (PO on :50000, S/4 on :8000; Ctrl+C stops)
    python3 scripts/sap-mock.py --user WO_MON --password Test.1234 --deny ARFCSSTATE

Then in Watchover, Ayarlar › Veri kaynakları:
    SAP PO / PI          http://<this server's IP>:50000                               Basic, the user / password above
    SAP S/4HANA / ABAP   http://<this server's IP>:8000/sap/bc/soap/rfc?sap-client=100  Basic, the same

  * PO: the Adapter Engine's getMessageList (AdapterMessageMonitoring): one message every 15 s over six interfaces (SOAP, IDoc,
    REST, JDBC, File, sync request + response), a few errors; between :20 and :30 of every hour the e-invoice interface fails.
  * S/4: RFC_READ_TABLE over SOAP-RFC: TBTCO (an aborted job every 20 min, one long-running job), SNAP_BEG (a short dump every
    25 min), EDIDC (an IDoc every 2 min, some in error), ARFCSSTATE (a stuck tRFC every 30 min); --deny TABLE answers that table
    with NOT_AUTHORIZED, as a user without S_TABU_NAM on it would get.

The same time always gives the same messages (ids come from the time slot), so repeated polls see the same records. Only the
standard library; Python 3.9+ (RHEL 9's python3). Test use only: plain http, no payloads, nothing stored.
"""
import argparse
import base64
import hashlib
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UTC = timezone.utc
NS_ID = uuid.UUID("6f1d3c2e-7a55-4b8e-9d0a-5ad0c0ffee01")

PO_STEP = 15                       # seconds between messages
PO_FLOWS = [                       # sender, receiver, interface, adapter connection, typical ms
    ("BS_ECC_PRD", "BS_CRM_PRD", "SI_Order_Out", "SOAP_http://sap.com/xi/XI/System", 230),
    ("BS_S4_PRD", "BS_WMS", "ORDERS05", "IDoc_AAE_http://sap.com/xi/XI/System", 900),
    ("BS_CRM_PRD", "BS_S4_PRD", "SI_Customer_Sync", "REST_http://sap.com/xi/XI/System", 340),
    ("BS_S4_PRD", "BS_EINVOICE", "SI_EInvoice_Out", "SOAP_http://sap.com/xi/XI/System", 1200),
    ("BS_S4_PRD", "BS_DWH", "SI_Sales_To_DWH", "JDBC_http://sap.com/xi/XI/System", 2100),
    ("BS_BANK", "BS_S4_PRD", "SI_Payment_File_In", "File_http://sap.com/xi/XI/System", 650),
]
PO_ERRORS = {
    "SI_EInvoice_Out": "SOAP: response message contains an error XIAdapter/HTTP/ADAPTER.HTTP_EXCEPTION - HTTP 503 Service Unavailable",
    "ORDERS05": "IDOC_ADAPTER: receiver port SAPWMS not maintained",
    "SI_Sales_To_DWH": "JDBC: Error processing request in sax parser: Error when executing statement: ORA-00001 unique constraint violated",
    "SI_Order_Out": "MAPPING: com.sap.aii.mappingtool.tf7.MessageMappingException: Cannot produce target element /ns0:Order/Item",
}


def h(*parts) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"


def po_messages(since: datetime, until: datetime, cap: int) -> list:
    out = []
    n0, n1 = int(since.timestamp()) // PO_STEP, int(until.timestamp()) // PO_STEP
    for n in range(max(n0, n1 - 5000), n1 + 1):
        start = datetime.fromtimestamp(n * PO_STEP, UTC)
        if not since <= start <= until:
            continue
        snd, rcv, iface, conn, ms = PO_FLOWS[n % len(PO_FLOWS)]
        ms = int(ms * (0.6 + (h(n, "ms") % 100) / 60))
        status, err = "success", ""
        storm = iface == "SI_EInvoice_Out" and 20 <= start.minute < 30
        if storm or (iface in PO_ERRORS and h(n, "err") % 100 < 6):
            status, err = "systemError", PO_ERRORS.get(iface, "")
        elif h(n, "hold") % 100 < 3:
            status = "holding"
        mid = str(uuid.uuid5(NS_ID, f"po-{n}"))
        m = {"id": mid, "conn": conn, "iface": iface, "snd": snd, "rcv": rcv, "status": status, "err": err, "start": start,
             "end": start + timedelta(milliseconds=ms), "qos": "BE" if iface == "SI_Customer_Sync" else "EO", "ref": ""}
        out.append(m)
        if iface == "SI_Customer_Sync":         # synchronous: the response message refers to the request
            out.append({**m, "id": str(uuid.uuid5(NS_ID, f"po-{n}-resp")), "snd": rcv, "rcv": snd, "iface": "SI_Customer_Sync_Resp",
                        "ref": mid, "start": m["end"], "end": m["end"] + timedelta(milliseconds=40), "status": "success", "err": ""})
    return out[-cap:]


def po_answer(body: bytes) -> bytes:
    get = lambda tag: (re.search(rb"<[\w:]*" + tag + rb">([^<]+)<", body) or [None, b""])[1].decode()   # noqa: E731
    now = datetime.now(UTC)
    try:
        since = datetime.fromisoformat(get(b"fromTime").replace("Z", "+00:00"))
        until = min(datetime.fromisoformat(get(b"toTime").replace("Z", "+00:00")), now)
    except ValueError:
        since, until = now - timedelta(minutes=15), now
    cap = int(get(b"maxMessages") or 1000)
    items = "".join(f"""
   <rn2:AdapterFrameworkData>
    <rn2:connectionName>{m['conn']}</rn2:connectionName><rn2:direction>OUTBOUND</rn2:direction>
    <rn2:endTime>{iso(m['end'])}</rn2:endTime><rn2:errorLabel>{m['err']}</rn2:errorLabel>
    <rn2:interface><rn3:name>{m['iface']}</rn3:name><rn3:namespace>urn:turkcell:test</rn3:namespace></rn2:interface>
    <rn2:messageID>{m['id']}</rn2:messageID><rn2:messageKey>{m['id']}\\OUTBOUND\\0</rn2:messageKey>
    <rn2:qualityOfService>{m['qos']}</rn2:qualityOfService>
    <rn2:receiverName>{m['rcv']}</rn2:receiverName><rn2:receiverParty/>
    <rn2:refToMessageId>{m['ref']}</rn2:refToMessageId>
    <rn2:senderName>{m['snd']}</rn2:senderName><rn2:senderParty/>
    <rn2:startTime>{iso(m['start'])}</rn2:startTime><rn2:status>{m['status']}</rn2:status>
   </rn2:AdapterFrameworkData>""" for m in po_messages(since, until, cap))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body>
 <rpl:getMessageListResponse xmlns:rpl="urn:AdapterMessageMonitoringVi"><rpl:Response xmlns:rn2="urn:com.sap.aii.mdt.server.adapterframework.ws"
   xmlns:rn3="urn:com.sap.aii.mdt.api.data"><rn2:list>{items}</rn2:list></rpl:Response></rpl:getMessageListResponse>
</SOAP-ENV:Body></SOAP-ENV:Envelope>""".encode()


# ------------------------------------------------------------------------------------------------------------------ S/4
TZ = timezone(timedelta(hours=3))   # Europe/Istanbul, the SAP system's local time
JOBS = ["ZFI_DAILY_CLOSE", "ZSD_BILLING_RUN", "ZMM_MRP_NIGHTLY", "ZHR_TIME_EVAL", "ZCO_SETTLEMENT"]
DUMPS = ["TIME_OUT", "DBSQL_DUPLICATE_KEY_ERROR", "MESSAGE_TYPE_X", "CONVT_NO_NUMBER", "TSV_TNEW_PAGE_ALLOC_FAILED"]
USERS = ["BATCH01", "BATCH02", "DEVELOPER", "AYSE.K", "MEHMET.T", "WF-BATCH"]
IDOCS = [("2", "ORDERS", "ORDERS05", "LS_CRM", "S4PCLNT100"), ("1", "INVOIC", "INVOIC02", "S4PCLNT100", "LS_EINVOICE"),
         ("1", "DESADV", "DESADV01", "S4PCLNT100", "LS_WMS"), ("2", "MATMAS", "MATMAS05", "LS_MDG", "S4PCLNT100")]
TRFC = [("BW_PRD_CLNT100", "RSAR_TRFC_DATA_RECEIVED"), ("CRM_PRD", "BAPI_BUPA_CENTRAL_CHANGE")]


def d8(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%Y%m%d")


def t6(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H%M%S")


def slots(day0: datetime, now: datetime, step_s: int):
    n0, n1 = int(day0.timestamp()) // step_s, int(now.timestamp()) // step_s
    for n in range(n0, n1 + 1):
        t = datetime.fromtimestamp(n * step_s, UTC)
        if day0 <= t <= now:
            yield n, t


def s4_records(table: str, day: str) -> list:
    now = datetime.now(UTC)
    try:
        day0 = datetime.strptime(day, "%Y%m%d").replace(tzinfo=TZ)
    except ValueError:
        day0 = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    day0 = max(day0, now - timedelta(days=2))
    out = []
    if table == "TBTCO":
        for n, t in slots(day0, now, 1200):
            if h(n, "job") % 100 < 45:
                job, user = JOBS[n % len(JOBS)], USERS[n % 2]
                end = t + timedelta(minutes=3 + h(n, "jd") % 9)
                if end <= now:
                    out.append({"JOBNAME": job, "JOBCOUNT": f"{n % 10**8:08d}", "SDLUNAME": user, "STATUS": "A", "STRTDATE": d8(t),
                                "STRTTIME": t6(t), "ENDDATE": d8(end), "ENDTIME": t6(end)})
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        long_start = hour0 - timedelta(minutes=95 if now.hour % 2 else 35)    # every other hour it is past the 60 min threshold
        out.append({"JOBNAME": "ZMM_STOCK_SYNC", "JOBCOUNT": f"{int(long_start.timestamp()) % 10**8:08d}", "SDLUNAME": "BATCH02",
                    "STATUS": "R", "STRTDATE": d8(long_start), "STRTTIME": t6(long_start), "ENDDATE": "00000000", "ENDTIME": "000000"})
        quick = now - timedelta(minutes=4)
        out.append({"JOBNAME": "ZSD_QUICK_CHECK", "JOBCOUNT": "00000042", "SDLUNAME": "BATCH01", "STATUS": "R", "STRTDATE": d8(quick),
                    "STRTTIME": t6(quick), "ENDDATE": "00000000", "ENDTIME": "000000"})
    elif table == "SNAP_BEG":
        for n, t in slots(day0, now, 1500):
            err, user = DUMPS[h(n, "dump") % len(DUMPS)], USERS[2 + h(n, "du") % 4]
            out.append({"DATUM": d8(t), "UZEIT": t6(t), "AHOST": f"s4app0{1 + n % 2}", "UNAME": user, "MANDT": "100",
                        "FLIST": f"FC{len(err):03d}{err} AP008ZPROG_{n % 7}"})
    elif table == "EDIDC":
        for n, t in slots(day0, now, 120):
            direct, mestyp, idoctp, snd, rcv = IDOCS[n % len(IDOCS)]
            bad = h(n, "idoc") % 100 < 8
            status = ("51" if direct == "2" else "02") if bad else ("53" if direct == "2" else "03")
            out.append({"DOCNUM": f"{n % 10**16:016d}", "STATUS": status, "DIRECT": direct, "MESTYP": mestyp, "IDOCTP": idoctp,
                        "SNDPRN": snd, "RCVPRN": rcv, "UPDDAT": d8(t), "UPDTIM": t6(t)})
    elif table == "ARFCSSTATE":
        for n, t in slots(day0, now, 1800):
            if h(n, "trfc") % 100 < 60:
                dest, fn = TRFC[n % len(TRFC)]
                out.append({"ARFCDEST": dest, "ARFCSTATE": "SYSFAIL" if n % 3 else "CPICERR", "ARFCFNAM": fn, "ARFCDATUM": d8(t),
                            "ARFCUZEIT": t6(t), "ARFCMSG": "Connection closed (no data)" if n % 3 == 0 else "Name or password is incorrect"})
    return out


def fault(text: str) -> bytes:
    return f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body><SOAP-ENV:Fault>
<faultcode>SOAP-ENV:Server</faultcode><faultstring>{text}</faultstring></SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>""".encode()


def s4_answer(body: bytes, deny: set) -> bytes:
    table = (re.search(rb"<QUERY_TABLE>(\w+)</QUERY_TABLE>", body) or [None, b""])[1].decode()
    if table in deny:
        return fault("NOT_AUTHORIZED")
    if table not in ("TBTCO", "SNAP_BEG", "EDIDC", "ARFCSSTATE"):
        return fault("TABLE_NOT_AVAILABLE")
    fields = [f.decode() for f in re.findall(rb"<FIELDNAME>(\w+)</FIELDNAME>", body)]
    where = " ".join(x.decode() for x in re.findall(rb"<TEXT>([^<]*)</TEXT>", body))
    day = (re.search(r">= '(\d{8})'", where) or [None, ""])[1]
    rowcount = int((re.search(rb"<ROWCOUNT>(\d+)</ROWCOUNT>", body) or [None, b"1000"])[1])
    recs = s4_records(table, day)            # TBTCO / ARFCSSTATE hold only the statuses Watchover's WHERE asks for
    recs = recs[-rowcount:] if rowcount else recs
    f = "".join(f"<item><FIELDNAME>{x}</FIELDNAME><OFFSET>0</OFFSET><LENGTH>40</LENGTH><TYPE>C</TYPE></item>" for x in fields)
    d = "".join("<item><WA>" + "|".join(str(r.get(x, "")) for x in fields) + "</WA></item>" for r in recs)
    return f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body>
<rfc:RFC_READ_TABLE.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions"><DATA>{d}</DATA><FIELDS>{f}</FIELDS><OPTIONS/></rfc:RFC_READ_TABLE.Response>
</SOAP-ENV:Body></SOAP-ENV:Envelope>""".encode()


# ------------------------------------------------------------------------------------------------------------------ server
def why(got, want: str) -> str:
    """Why a request was refused, for the operator at this terminal: the user name that came (never the password)."""
    if not got:
        return "kimlik gönderilmedi (kaynakta Kimlik 'basic' ve kullanıcı / parola dolu olmalı)"
    try:
        u, _, p = base64.b64decode(got.split(" ", 1)[1]).decode("utf-8", "replace").partition(":")
        wu, _, wp = base64.b64decode(want.split(" ", 1)[1]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return "Authorization başlığı okunamadı"
    if u != wu:
        return f"kullanıcı '{u}' geldi, beklenen '{wu}'"
    return f"kullanıcı doğru ('{u}'), parola yanlış ({len(p)} karakter geldi, beklenen {len(wp)})" if p else f"kullanıcı doğru ('{u}'), parola boş geldi"


def handler(kind: str, auth: str, deny: set):
    class H(BaseHTTPRequestHandler):
        server_version = "SAP NetWeaver Application Server (mock)"

        def do_POST(self):
            n = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            body = self.rfile.read(n)
            if auth and self.headers.get("Authorization") != auth:
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="SAP"'); self.send_header("Content-Length", "0")
                self.end_headers(); print(f"{datetime.now().strftime('%H:%M:%S')} {kind}: 401, {why(self.headers.get('Authorization'), auth)}", flush=True); return
            if kind == "PO" and "AdapterMessageMonitoring" not in self.path:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            if kind == "S4" and not self.path.startswith("/sap/bc/soap/rfc"):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            out = po_answer(body) if kind == "PO" else s4_answer(body, deny)
            self.send_response(500 if b"<SOAP-ENV:Fault>" in out else 200)
            self.send_header("Content-Type", "text/xml; charset=utf-8"); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)
            what = (re.search(rb"<QUERY_TABLE>(\w+)<", body) or [None, b"getMessageList"])[1].decode()
            print(f"{datetime.now().strftime('%H:%M:%S')} {kind} {what}: {out.count(b'<rn2:AdapterFrameworkData>') or out.count(b'<WA>')} kayıt"
                  f"{' (NOT_AUTHORIZED)' if b'NOT_AUTHORIZED' in out else ''}", flush=True)

        def do_GET(self):
            out = f"SAP {kind} mock: POST only\n".encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="SAP PO + S/4HANA stand-in for testing Watchover's SAP sources")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--po-port", type=int, default=50000)
    ap.add_argument("--s4-port", type=int, default=8000)
    ap.add_argument("--user", default="", help="Basic auth user (empty: any)")
    ap.add_argument("--password", default="")
    ap.add_argument("--deny", default="", help="comma-separated S/4 tables answered with NOT_AUTHORIZED, e.g. ARFCSSTATE")
    a = ap.parse_args()
    auth = "Basic " + base64.b64encode(f"{a.user}:{a.password}".encode()).decode() if a.user else ""
    deny = {t.strip().upper() for t in a.deny.split(",") if t.strip()}
    servers = []
    for kind, port in (("PO", a.po_port), ("S4", a.s4_port)):
        try:
            servers.append(ThreadingHTTPServer((a.host, port), handler(kind, auth, deny)))
        except OSError as e:
            for s in servers:
                s.server_close()
            print(f"Port {port} kullanımda ({e.strerror}). Önceki taklit sunucu hâlâ çalışıyor olabilir: sudo pkill -f sap-mock.py\n"
                  f"Portu kim tutuyor: sudo ss -ltnp 'sport = :{port}'   (başka bir servisse: --{kind.lower()}-port ile başka port verin)", file=sys.stderr)
            return 1
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"SAP PO  : http://<bu sunucu>:{a.po_port}   (getMessageList)")
    print(f"SAP S/4 : http://<bu sunucu>:{a.s4_port}/sap/bc/soap/rfc?sap-client=100   (RFC_READ_TABLE)")
    print(f"Kimlik  : {a.user + ' / ' + ('*' * len(a.password)) if a.user else 'herhangi'}{'   yetkisiz tablo: ' + ', '.join(sorted(deny)) if deny else ''}")
    print("Durdurmak için Ctrl+C", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    for s in servers:
        s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
