"""SAP S/4HANA / ABAP source (v1.62): RFC_READ_TABLE over the SOAP-RFC service -> aborted / long jobs, short dumps, IDoc errors,
stuck tRFC calls as log lines, IDoc partners and RFC destinations as integration-map edges; one table failing never stops the rest."""
import http.server
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

from watchover import sap_s4

TZ = sap_s4._tz("Europe/Istanbul")
NOW = datetime.now(TZ).replace(microsecond=0)


def tables(now: datetime) -> dict:
    """The stand-in's rows, timed 10 minutes / 3 hours before `now`."""
    D, T = now.strftime("%Y%m%d"), (now - timedelta(minutes=10)).strftime("%H%M%S")
    EARLY = (now - timedelta(hours=3)).strftime("%H%M%S")
    return {
        "TBTCO": [f"ZFI_DAILY_CLOSE|12345678|BATCH01|A|{D}|{EARLY}|{D}|{T}", f"ZMM_STOCK_SYNC|22345678|BATCH02|R|{D}|{EARLY}|00000000|000000",
                  f"ZSD_QUICK|32345678|BATCH03|R|{D}|{T}|00000000|000000"],
        "SNAP_BEG": [f"{D}|{T}|s4app01|DEVELOPER|100|FC010TIME_OUT AP005ZPROG"],
        "EDIDC": [f"0000000000123456|51|2|ORDERS|ORDERS05|LS_CRM|S4PCLNT100|{D}|{T}", f"0000000000123457|53|2|ORDERS|ORDERS05|LS_CRM|S4PCLNT100|{D}|{T}",
                  f"0000000000123458|03|1|INVOIC|INVOIC02|S4PCLNT100|LS_EINVOICE|{D}|{T}"],
    }


TABLES = tables(NOW)


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
    rows = sap_s4.fetch(call, "s4app01", NOW - timedelta(minutes=15), TZ)
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


def test_a_later_poll_does_not_resend_the_day():
    """The tables are read by date: a poll 30 s after the last one keeps only what changed since (and the running job, whose
    line reads the same every time so the poller's hash set drops it), and counts no map edge twice."""
    call = lambda xml: answer(re.search(rb"<QUERY_TABLE>(\w+)</QUERY_TABLE>", xml).group(1).decode())   # noqa: E731
    first = [r for r in sap_s4.fetch(call, "s4app01", NOW - timedelta(minutes=15), TZ) if r.get("s4.kind") == "job"]
    later = sap_s4.fetch(call, "s4app01", NOW - timedelta(seconds=30), TZ)
    assert [r["s4.job"] for r in later if r.get("s4.kind") == "job"] == ["ZMM_STOCK_SYNC"]
    assert [r["msg"] for r in later if r.get("s4.kind") == "job"] == [first[1]["msg"]]
    assert not [r for r in later if r.get("s4.kind") in ("idoc", "dump")] and not [r for r in later if "flow.dir" in r]


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
    monkeypatch.setattr(sys.modules[__name__], "TABLES", tables(datetime.now(TZ)))   # the poller reads up to now: rows timed at import
                                                                       # time were outside its window minutes later in a full run
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


def test_the_test_server_stand_in_speaks_both_protocols():
    """scripts/sap-mock.py (a test server without SAP): its PO and S/4 answers read like the real ones, the same time window
    gives the same message ids, and a denied table comes back as NOT_AUTHORIZED."""
    import importlib.util
    import pathlib
    from watchover import sap_po
    spec = importlib.util.spec_from_file_location("sap_mock", pathlib.Path(__file__).parent.parent / "scripts" / "sap-mock.py")
    mock = importlib.util.module_from_spec(spec); spec.loader.exec_module(mock)
    now = datetime.now(timezone.utc)
    req = sap_po.request_xml(now - timedelta(minutes=10), now)
    a, b = sap_po.parse_messages(mock.po_answer(req)), sap_po.parse_messages(mock.po_answer(req))
    assert len(a) >= 40 and [m["id"] for m in a] == [m["id"] for m in b] and {m["adapter"] for m in a} >= {"SOAP", "IDoc", "JDBC"}
    rows = sap_s4.fetch(lambda xml: mock.s4_answer(xml, {"ARFCSSTATE"}), "s4app01", now - timedelta(hours=3), TZ)
    kinds = {r.get("s4.kind") for r in rows}
    assert {"dump", "monitor"} <= kinds and any("flow.dir" in r for r in rows)
    assert any(r.get("s4.table") == "ARFCSSTATE" for r in rows)


def test_nothing_readable_fails_the_poll():
    """A wrong password (401 on every table) is a failing source with the reason, not four warning lines and a green status."""
    def call(xml):
        raise OSError("HTTP Error 401: Unauthorized")
    try:
        sap_s4.fetch(call, "s4app01", NOW - timedelta(minutes=5), TZ)
        raise AssertionError("not raised")
    except RuntimeError as e:
        assert "401" in str(e) and "TBTCO" in str(e)
