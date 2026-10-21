"""SAP S/4HANA / ABAP source (v1.62): RFC_READ_TABLE over the SOAP-RFC service -> aborted / long jobs, short dumps, IDoc errors,
stuck tRFC calls as log lines, IDoc partners and RFC destinations as integration-map edges; one table failing never stops the rest."""
import http.server
import re
import threading
from datetime import datetime, timedelta, timezone

from watchover import sap_s4

TZ = sap_s4._tz("Europe/Istanbul")
NOW = datetime.now(TZ).replace(microsecond=0)
D, T = NOW.strftime("%Y%m%d"), (NOW - timedelta(minutes=10)).strftime("%H%M%S")
EARLY = (NOW - timedelta(hours=3)).strftime("%H%M%S")

TABLES = {
    "TBTCO": [f"ZFI_DAILY_CLOSE|12345678|BATCH01|A|{D}|{EARLY}|{D}|{T}", f"ZMM_STOCK_SYNC|22345678|BATCH02|R|{D}|{EARLY}|00000000|000000",
              f"ZSD_QUICK|32345678|BATCH03|R|{D}|{T}|00000000|000000"],
    "SNAP_BEG": [f"{D}|{T}|s4app01|DEVELOPER|100|FC010TIME_OUT AP005ZPROG"],
    "EDIDC": [f"0000000000123456|51|2|ORDERS|ORDERS05|LS_CRM|S4PCLNT100|{D}|{T}", f"0000000000123457|53|2|ORDERS|ORDERS05|LS_CRM|S4PCLNT100|{D}|{T}",
              f"0000000000123458|03|1|INVOIC|INVOIC02|S4PCLNT100|LS_EINVOICE|{D}|{T}"],
}


def answer(table: str) -> bytes:
    if table == "ARFCSSTATE":                                              # this user may not read SM58's table
        return b"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body><SOAP-ENV:Fault>
<faultcode>SOAP-ENV:Server</faultcode><faultstring>NOT_AUTHORIZED</faultstring></SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
    fields = sap_s4.QUERIES[{v[0]: k for k, v in sap_s4.QUERIES.items()}[table]][1]
    f = "".join(f"<item><FIELDNAME>{x}</FIELDNAME><OFFSET>0</OFFSET><LENGTH>10</LENGTH><TYPE>C</TYPE></item>" for x in fields)
    d = "".join(f"<item><WA>{w}</WA></item>" for w in TABLES.get(table, []))
    return f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body>
<rfc:RFC_READ_TABLE.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions"><DATA>{d}</DATA><FIELDS>{f}</FIELDS><OPTIONS/></rfc:RFC_READ_TABLE.Response>
</SOAP-ENV:Body></SOAP-ENV:Envelope>""".encode()


def test_where_clauses_and_parsing():
    req = sap_s4.read_table_xml("TBTCO", sap_s4.QUERIES["jobs"][1], sap_s4.where_for("jobs", NOW, TZ))
    assert b"<QUERY_TABLE>TBTCO</QUERY_TABLE>" in req and b"STATUS = 'A'" in req and all(len(x) <= 72 for x in re.findall(rb"<TEXT>(.*?)</TEXT>", req))
    rows = sap_s4.parse_read_table(answer("TBTCO"), sap_s4.QUERIES["jobs"][1])
    assert rows[0]["JOBNAME"] == "ZFI_DAILY_CLOSE" and rows[0]["STATUS"] == "A"
    try:
        sap_s4.parse_read_table(answer("ARFCSSTATE"), ())
        raise AssertionError("fault not raised")
    except ValueError as e:
        assert "NOT_AUTHORIZED" in str(e)
    assert sap_s4.dump_error("FC010TIME_OUT AP005ZPROG") == "TIME_OUT"


def test_findings_and_edges():
    calls = []

    def call(xml):
        t = re.search(rb"<QUERY_TABLE>(\w+)</QUERY_TABLE>", xml).group(1).decode()
        calls.append(t)
        return answer(t)
    rows = sap_s4.fetch(call, "s4app01", NOW - timedelta(minutes=5), TZ)
    assert calls == ["TBTCO", "SNAP_BEG", "EDIDC", "ARFCSSTATE"]
    by = {}
    for r in rows:
        by.setdefault(r.get("s4.kind") or ("flow" if "flow.dir" in r else "?"), []).append(r)
    assert [r["s4.job"] for r in by["job"]] == ["ZFI_DAILY_CLOSE", "ZMM_STOCK_SYNC"]             # aborted + running > 60 min; the quick one not
    assert by["job"][0]["level"] == "ERROR" and "aborted after" in by["job"][0]["msg"] and by["job"][0]["sap.user"] == "BATCH01"
    assert by["dump"][0]["s4.dump"] == "TIME_OUT" and by["dump"][0]["sap.user"] == "DEVELOPER"
    assert [r["s4.idoc"] for r in by["idoc"]] == ["0000000000123456"] and by["idoc"][0]["s4.partner"] == "LS_CRM"   # 53 and 03 are fine
    assert by["monitor"][0]["level"] == "WARN" and "ARFCSSTATE" in by["monitor"][0]["msg"]      # one table refused, the rest still read
    edges = {(r["host"], r["flow.remote"], r["flow.count"]) for r in by["flow"]}
    assert edges == {("LS_CRM", "s4app01:3300", 2), ("s4app01", "LS_EINVOICE:3300", 1)}


class _S4(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        out = answer(re.search(rb"<QUERY_TABLE>(\w+)</QUERY_TABLE>", body).group(1).decode())
        self.send_response(500 if b"Fault" in out else 200); self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_source_end_to_end(tmp_path, monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home")); monkeypatch.setenv("TZ", "Europe/Istanbul")
    from watchover import sources as S
    from watchover.flows import FlowStore
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _S4)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        kb = Knowledge(str(tmp_path / "k.db"))
        store, live, flows = S.SourceStore(kb), LiveStore(), FlowStore(kb)
        live.on_flows = flows.add
        src = store.add(S.Source(None, "s4-prd", "sap-s4", f"http://127.0.0.1:{srv.server_address[1]}/sap/bc/soap/rfc?sap-client=100",
                                 auth="basic", user="WO_MON", secret="pw", env="prod"))
        n = S.Poller(store, live).poll_one(store.get(src.id))
        assert n >= 5                                                      # 2 jobs, 1 dump, 1 IDoc, 1 monitor warning (+ edges to the flow table)
        assert {r["endpoint"] for r in kb._exec("SELECT endpoint FROM net_flows")} == {"s4app01:3300", "LS_EINVOICE:3300"} or \
            {r["endpoint"] for r in kb._exec("SELECT endpoint FROM net_flows")} == {"127.0.0.1:3300", "LS_EINVOICE:3300"}
    finally:
        srv.shutdown()
