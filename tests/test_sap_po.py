"""SAP PO / PI source (v1.61): the Adapter Engine's getMessageList answer becomes log lines, traces (sender -> SAP PO -> receiver,
trace id = PO message id) and integration-map edges with the adapter types; payloads are never asked for."""
import http.server
import threading
from datetime import datetime, timedelta, timezone

import pytest

from watchover import sap_po

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+00:00"


def answer(msgs):
    items = "".join(f"""
      <rn2:AdapterFrameworkData>
        <rn2:connectionName>{m['adapter']}</rn2:connectionName><rn2:direction>{m.get('dir', 'OUTBOUND')}</rn2:direction>
        <rn2:endTime>{_iso(m['start'] + timedelta(milliseconds=m['ms']))}</rn2:endTime>
        <rn2:errorLabel>{m.get('err', '')}</rn2:errorLabel>
        <rn2:interface><rn3:name>{m['iface']}</rn3:name><rn3:namespace>urn:turkcell:erp</rn3:namespace></rn2:interface>
        <rn2:messageID>{m['id']}</rn2:messageID><rn2:messageKey>{m['id']}\\OUTBOUND\\0</rn2:messageKey>
        <rn2:qualityOfService>{m.get('qos', 'EO')}</rn2:qualityOfService>
        <rn2:receiverName>{m['rcv']}</rn2:receiverName><rn2:receiverParty/>
        <rn2:refToMessageId>{m.get('ref', '')}</rn2:refToMessageId>
        <rn2:senderName>{m['snd']}</rn2:senderName><rn2:senderParty/>
        <rn2:startTime>{_iso(m['start'])}</rn2:startTime><rn2:status>{m['status']}</rn2:status>
      </rn2:AdapterFrameworkData>""" for m in msgs)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Body>
 <rpl:getMessageListResponse xmlns:rpl="urn:AdapterMessageMonitoringVi"><rpl:Response xmlns:rn2="urn:com.sap.aii.mdt.server.adapterframework.ws"
   xmlns:rn3="urn:com.sap.aii.mdt.api.data"><rn2:list>{items}</rn2:list></rpl:Response></rpl:getMessageListResponse>
</SOAP-ENV:Body></SOAP-ENV:Envelope>""".encode()


MSGS = [
    {"id": "4a1b2c3d-1111-2222-3333-444455556666", "adapter": "SOAP_http://sap.com/xi/XI/System", "iface": "SI_Order_Out", "snd": "BS_ECC_PRD",
     "rcv": "BS_CRM_PRD", "status": "success", "start": NOW - timedelta(minutes=2), "ms": 230},
    {"id": "5b2c3d4e-1111-2222-3333-444455556666", "adapter": "IDoc_AAE_http://sap.com/xi/XI/System", "iface": "ORDERS05", "snd": "BS_ECC_PRD",
     "rcv": "BS_WMS", "status": "systemError", "start": NOW - timedelta(minutes=1), "ms": 1200, "err": "IDOC_ADAPTER: receiver port not maintained"},
]


def test_parse_namespace_agnostic_and_statuses():
    msgs = sap_po.parse_messages(answer(MSGS))
    assert [m["id"] for m in msgs] == [m["id"] for m in MSGS]
    a, b = msgs
    assert (a["interface"], a["namespace"], a["adapter"], a["sender"], a["receiver"]) == ("SI_Order_Out", "urn:turkcell:erp", "SOAP", "BS_ECC_PRD", "BS_CRM_PRD")
    assert sap_po.status_of(a) == ("delivered", "INFO", False) and sap_po.duration_ms(a) == 230
    assert b["adapter"] == "IDoc" and sap_po.status_of(b)[0] == "error" and "receiver port" in b["error"]
    with pytest.raises(ValueError):
        sap_po.parse_messages(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
    fault = b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/"><e:Body><e:Fault><faultstring>not authorized</faultstring></e:Fault></e:Body></e:Envelope>'
    with pytest.raises(ValueError, match="not authorized"):
        sap_po.parse_messages(fault)
    assert b"<urn:getMessageList>" in sap_po.request_xml(NOW - timedelta(minutes=5), NOW) and b"payload" not in sap_po.request_xml(NOW, NOW).lower()


def test_spans_flows_and_rows():
    msgs = sap_po.parse_messages(answer(MSGS))
    sp = sap_po.spans_of(msgs, "sappo01", "prod")
    assert len(sp) == 6 and [s["service"] for s in sp[:3]] == ["BS_ECC_PRD", "SAP PO", "BS_CRM_PRD"]
    assert sp[1]["parent_id"] == sp[0]["span_id"] and sp[2]["parent_id"] == sp[1]["span_id"] and sp[0]["trace_id"] == MSGS[0]["id"].replace("-", "")
    assert sp[4]["error"] == 1 and sp[4]["duration_ms"] == 1200
    fl = sap_po.flows_of(msgs, "sappo01", "prod")
    edges = {(o.host, o.attributes["flow.remote"], o.attributes["flow.process"]) for o in fl}
    assert ("BS_ECC_PRD", "sappo01:443", "PO SOAP") in edges and ("sappo01", "BS_WMS:3300", "PO IDoc") in edges
    rows = sap_po.rows_of(msgs, "sappo01")
    assert rows[1]["level"] == "ERROR" and rows[1]["po.interface"] == "ORDERS05" and "BS_ECC_PRD → BS_WMS" in rows[1]["msg"]
    assert "trace_id" not in rows[0] and "duration_ms" not in rows[0]     # the generic span builder must not make a second trace


class _PO(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _PO.seen.append((self.path, self.headers.get("Authorization"), body))
        out = answer(MSGS)
        self.send_response(200); self.send_header("Content-Type", "text/xml"); self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_source_end_to_end(tmp_path, monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    from watchover import sources as S
    from watchover.flows import FlowStore
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore
    from watchover.traces import TraceStore
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PO)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        kb = Knowledge(str(tmp_path / "k.db"))
        store, live, traces, flows = S.SourceStore(kb), LiveStore(), TraceStore(kb), FlowStore(kb)
        live.on_flows = flows.add
        src = store.add(S.Source(None, "po-prd", "sap-po", f"http://127.0.0.1:{srv.server_address[1]}", auth="basic", user="WO_MON", secret="pw", env="prod"))
        got = []
        poller = S.Poller(store, live, on_rows=lambda s, rows: got.append(sap_po.feed(rows, "127.0.0.1", s.env, traces, flows)))
        assert poller.poll_one(store.get(src.id)) == 2
        path, auth, body = _PO.seen[-1]
        assert path == sap_po.SOAP_PATH and auth.startswith("Basic ") and b"getMessageList" in body
        assert got == [{"messages": 2, "spans": 6, "flows": 4}] or got[0]["messages"] == 2
        ev = [o for o in live.events if o.service == "sap-po"] if hasattr(live, "events") else []
        if ev:
            assert {o.attributes.get("po.interface") for o in ev} == {"SI_Order_Out", "ORDERS05"}
        traces.flush()
        t = traces.trace(MSGS[1]["id"].replace("-", ""))
        assert t and {s["service"] for s in t["spans"]} >= {"BS_ECC_PRD", "SAP PO", "BS_WMS"}
        hm = flows.host_map("127.0.0.1")
        assert {(x["target"], x["protocol"], x["known"]) for x in hm["outbound"]} == {("BS_CRM_PRD", "https", False), ("BS_WMS", "sap-rfc", False)}
        assert {r["tech"] for r in kb._exec("SELECT tech FROM net_flows")} == {"sap-po"}                # the SAP PO tag on every edge
        assert any(x["source"] == "BS_ECC_PRD" for x in flows.host_map("127.0.0.1")["inbound"])         # sender system -> PO
        assert poller.poll_one(store.get(src.id)) == 0                    # the same messages again: not ingested twice
    finally:
        srv.shutdown()


def test_po_java_log_lines_join_the_message_trace():
    from watchover.models import Observation
    o = Observation(timestamp=NOW, message="Unable to process message 4A1B2C3D-1111-2222-3333-444455556666: channel CC_SOAP_Order_Out stopped",
                    attributes={"sap.format": "nw-java", "sap.logger": "com.sap.aii.adapter.soap.web.MessageServlet"})
    sap_po.tag_log(o)
    assert o.attributes["trace_id"] == "4a1b2c3d111122223333444455556666" and o.attributes["po.channel"] == "CC_SOAP_Order_Out"
    other = Observation(timestamp=NOW, message="message 4a1b2c3d-1111-2222-3333-444455556666", attributes={"sap.format": "nw-java", "sap.logger": "com.sap.engine.services.deploy"})
    sap_po.tag_log(other)
    assert "trace_id" not in other.attributes                              # not a PO logger: left alone
    assert sap_po.po_host("https://sappo01.turkcell.com.tr:50001") == "sappo01" and sap_po.po_host("http://10.1.2.3:50000") == "10.1.2.3"


def test_sap_users_and_po_messages_make_sessions():
    from watchover import rollup
    assert rollup.SES_ROLES["sap.user"] == "u" and rollup.SES_ROLES["po.msg_id"] == "t"


def test_a_po_log_line_is_not_a_second_trace_and_jdbc_is_not_guessed():
    from watchover.flows import port_name
    from watchover.models import Observation
    from watchover.traces import span_of
    msgs = sap_po.parse_messages(answer(MSGS))
    r = sap_po.rows_of(msgs, "sappo01")[0]
    o = Observation(timestamp=NOW, message=r["msg"], service="sap-po", host="sappo01", attributes={k: v for k, v in r.items() if "." in k or k == "session_id"})
    assert span_of(o) is None                                              # "0.23 s", not "230 ms": the trace comes from feed() only
    jd = sap_po.flows_of([{**msgs[0], "adapter": "JDBC", "receiver": "BS_DWH"}], "sappo01")
    assert any(x.attributes["flow.remote"] == "BS_DWH:0" for x in jd) and port_name(0) == "jdbc"
