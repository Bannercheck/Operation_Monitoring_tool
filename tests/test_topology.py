"""Readers and maps: SharePoint ULS and Keycloak SSO into the trace model, agent TCP flows (parsing /proc/net/tcp lines, server
side graph with inventory names), and the service topology from traces with explicit vs inferred edges."""
import importlib.util
import json
import random
from pathlib import Path

from watchover.db import Database
from watchover.flows import FlowStore, port_name
from watchover.live import LiveStore, simulate_batch, simulate_flows
from watchover.pipeline import ingest_bytes
from watchover.traces import TraceStore, span_of, store_for_observations

ROOT = Path(__file__).resolve().parents[1]


def test_sharepoint_uls():
    uls = ("Timestamp              \tProcess                                 \tTID   \tArea                          \tCategory                      \tEventID\tLevel     \tMessage \tCorrelation\n"
           "10/14/2026 10:00:01.12 \tw3wp.exe (0x1A2B)                       \t0x0F10\tSharePoint Foundation         \tRequest Management            \tadc7r\tHigh      \t"
           "Leaving Monitored Scope (Request (GET:https://portal/sites/hr/Pages/Bordro.aspx)). Execution Time=245.12\t0f8e2a9c-1111-2222-3333-444455556666\n"
           "10/14/2026 10:00:01.20 \tw3wp.exe (0x1A2B)                       \t0x0F10\tSharePoint Server             \tSearch                        \tx1\tUnexpected\tQuery failed\t0f8e2a9c-1111-2222-3333-444455556666\n")
    obs, _ = ingest_bytes("SP-20261014-1000.log", uls.encode())
    a, b = obs
    assert a.severity == "INFO" and b.severity == "ERROR"                                   # ULS "High" is routine, "Unexpected" the error
    sa, sb = span_of(a), span_of(b)
    assert sa["service"] == "sharepoint:Request Management" and sa["duration_ms"] == 245.12 and sa["operation"] == "GET /sites/hr/Pages/Bordro.aspx"
    assert sa["trace_id"] == sb["trace_id"] == "0f8e2a9c-1111-2222-3333-444455556666" and sb["kind"] == "log"


def test_keycloak_sso_journey(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_PSEUDO_KEY", "k")
    from datetime import datetime, timedelta
    t0 = datetime.now() - timedelta(minutes=10)                                 # within the spans' retention (72 h) whatever the day
    t1 = t0 + timedelta(minutes=3)
    kc = (f"{t0:%Y-%m-%d %H:%M:%S},345 INFO  [org.keycloak.events] (executor-thread-1) type=LOGIN, realmId=turkcell, clientId=ik-portal, userId=8f1d2c3b-aaaa, ipAddress=10.1.2.3, username=ayse.yilmaz, sessionId=5a6b7c8d-1111\n"
          f"{t1:%Y-%m-%d %H:%M:%S},345 INFO  [org.keycloak.events] (executor-thread-1) type=CODE_TO_TOKEN, realmId=turkcell, clientId=crm, userId=8f1d2c3b-aaaa, sessionId=5a6b7c8d-1111\n")
    obs, _ = ingest_bytes("keycloak.log", kc.encode())
    sp = [span_of(o) for o in obs]
    assert [s["operation"] for s in sp] == ["sso login → ik-portal", "sso code_to_token → crm"]
    assert sp[0]["session_id"] == sp[1]["session_id"] == "sso:5a6b7c8d-1111" and sp[0]["user_ref"] == sp[1]["user_ref"] != ""
    assert "ayse" not in json.dumps(sp, default=str) and "8f1d2c3b" not in json.dumps(sp, default=str) and all(o.message.startswith(("LOGIN", "CODE")) for o in obs)
    tr = TraceStore(Database(str(tmp_path / "t.db")), settings=lambda: {"trace_sample_pct": 0})
    tr.add_spans(sp)                                                                            # SSO steps are kept like user steps
    hits = tr.search(minutes=None, session="sso:5a6b7c8d-1111")
    assert len(hits) == 2


def test_agent_tcp_flows_parsing(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wo_agent", ROOT / "agent.py"); agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
    assert agent._hexaddr("0100007F:1F90") == "127.0.0.1:8080" and agent._hexaddr("0000000000000000FFFF0000370110AC:1538") == "172.16.1.55:5432"
    tcp = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
           "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1",
           "   1: 0A00000A:1F90 0B00000A:C350 01 00000000:00000000 00:00000000 00000000     0        0 112 1",     # inbound to :8080
           "   2: 0A00000A:D431 370110AC:1538 01 00000000:00000000 00:00000000 00000000     0        0 113 1",     # out to 172.16.1.55:5432
           "   3: 0A00000A:D432 370110AC:1538 01 00000000:00000000 00:00000000 00000000     0        0 114 1",
           "   4: 0100007F:D433 0100007F:1538 01 00000000:00000000 00:00000000 00000000     0        0 115 1"]     # loopback: skipped
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "\n".join(tcp) if str(self) == "/proc/net/tcp" else ("" if str(self) == "/proc/net/tcp6" else real(self, *a, **k)))
    monkeypatch.setattr(agent, "_inode_process", lambda: {"112": "java", "113": "java", "114": "java"})
    fl = {(f["flow.dir"], f.get("flow.remote") or f.get("flow.port")): f["flow.count"] for f in agent.tcp_flows()}
    assert fl == {("out", "172.16.1.55:5432"): 2, ("in", "8080"): 1}


def test_flow_store_graph_and_live_diversion(tmp_path):
    db = Database(str(tmp_path / "k.db"))
    db._exec("CREATE TABLE inventory (id INTEGER PRIMARY KEY, hostname TEXT, ip TEXT)")
    db._exec("INSERT INTO inventory (hostname, ip) VALUES ('db-01', '172.16.1.55')")
    fs = FlowStore(db)
    live = LiveStore(spool=str(tmp_path / "l.jsonl")); live.on_flows = fs.add
    before = len(live.snapshot())
    rng = random.Random(1)
    live.ingest("flows.jsonl", simulate_flows(rng), "sim"); live.ingest("flows.jsonl", simulate_flows(rng), "sim")
    assert len(live.snapshot()) == before                                                       # flows never become log events
    g = fs.graph(minutes=None)
    names = {n["id"]: n for n in g["nodes"]}
    assert names["db-01"]["kind"] == "db" and names["prd-api-01"]["agent"] and names["db-01"]["listens"][0]["port"] == "5432"
    e = next(e for e in g["edges"] if e["source"] == "prd-api-01" and e["target"] == "db-01")
    assert e["protocol"] == "postgres" and e["evidence"] == "flow" and e["samples"] == 2
    assert port_name(1521) == "oracle" and port_name(3200) == "sap-dialog" and port_name(12345) == "tcp/12345"


def test_topology_from_traces():
    rng = random.Random(2)
    raw = b"".join(simulate_batch(rng, 9, incident=True, traces=True) for _ in range(20))
    obs, _ = ingest_bytes("sim.jsonl", raw)
    t = store_for_observations(obs).topology(minutes=None)
    edges = {(e["source"], e["target"]): e for e in t["edges"]}
    assert ("api-gateway", "checkout-api") in edges or ("api-gateway", "search-api") in edges
    assert any(t2 == "db-01" for (_, t2) in edges)                                              # database calls land on the db host
    gw = next(e for (s, _), e in edges.items() if s == "api-gateway")
    assert gw["evidence"] == "explicit" and gw["calls"] > 0 and gw["p95_ms"] is not None
    kinds = {n["id"]: n["kind"] for n in t["nodes"]}
    assert kinds["db-01"] == "db" and kinds["api-gateway"] == "service"


def test_host_integration_map(tmp_path):
    db = Database(str(tmp_path / "k.db"))
    db._exec("CREATE TABLE inventory (id INTEGER PRIMARY KEY, hostname TEXT, ip TEXT)")
    db.executemany("INSERT INTO inventory (hostname, ip) VALUES (?, ?)", [("db-01", "172.16.1.55"), ("kafka-01", "172.16.1.70"), ("hr-db", "172.16.2.10")])
    fs = FlowStore(db)
    live = LiveStore(spool=str(tmp_path / "l.jsonl")); live.on_flows = fs.add
    live.ingest("flows.jsonl", simulate_flows(random.Random(3)), "sim")
    hosts = {h["host"]: h for h in fs.hosts(minutes=None)}
    assert hosts["prd-api-01"]["agent"] and hosts["prd-api-01"]["outbound"] == 2 and "postgres" in hosts["prd-api-01"]["protocols"]
    assert hosts["db-01"]["inbound"] >= 3                                                      # three hosts' flows end at its address (+ its listen)
    m = fs.host_map("prd-api-01", minutes=None)
    cats = {o["target"]: o["category"] for o in m["outbound"]}
    assert cats == {"db-01": "db", "kafka-01": "messaging"} and m["processes"] == ["java"]
    d = fs.host_map("db-01", minutes=None)
    assert d["ips"] == ["172.16.1.55"] and {i["source"] for i in d["inbound"]} == {"prd-api-01", "prd-api-02", "worker-01"}
    assert d["listens"][0]["protocol"] == "postgres" and d["listens"][0]["process"] == "postgres"
    w = fs.host_map("hr-app-01", minutes=None)
    assert {o["category"] for o in w["outbound"]} == {"db", "api"} and any(not o["known"] for o in w["outbound"])   # an unnamed external API by IP



def test_ips_under_hosts_and_application_integration_map(tmp_path):
    """Every host carries its known addresses (an unnamed peer: its own IP); an application's map folds its members' maps: what
    leaves or enters the member set is an integration (with the member it goes through), flows between members are internal."""
    db = Database(str(tmp_path / "k.db"))
    db._exec("CREATE TABLE inventory (id INTEGER PRIMARY KEY, hostname TEXT, ip TEXT)")
    db.executemany("INSERT INTO inventory (hostname, ip) VALUES (?, ?)", [("db-01", "172.16.1.55"), ("kafka-01", "172.16.1.70"), ("hr-db", "172.16.2.10")])
    fs = FlowStore(db)
    live = LiveStore(spool=str(tmp_path / "l.jsonl")); live.on_flows = fs.add
    live.ingest("flows.jsonl", simulate_flows(random.Random(3)), "sim")
    hosts = {h["host"]: h for h in fs.hosts(minutes=None)}
    assert hosts["db-01"]["ips"] == ["172.16.1.55"] and hosts["prd-api-01"]["ips"] == []
    assert all(h["ips"] == [h["host"]] for h in hosts.values() if h["host"][:1].isdigit())          # known only by its address
    d = fs.host_map("db-01", minutes=None)
    assert all("ips" in i for i in d["inbound"])
    a = fs.app_map("Ödeme", ["prd-api-01", "prd-api-02", "db-01"], minutes=None)
    assert a["app"] and [m["host"] for m in a["members"]] == ["db-01", "prd-api-01", "prd-api-02"] and a["ips"] == ["172.16.1.55"]
    assert {(e["source"], e["target"]) for e in a["internal"]} >= {("prd-api-01", "db-01")}                # inside the application
    assert all(o["target"] not in ("db-01", "prd-api-01", "prd-api-02") for o in a["outbound"])
    kafka = next(o for o in a["outbound"] if o["target"] == "kafka-01")
    assert kafka["category"] == "messaging" and set(kafka["via"]) <= {"prd-api-01", "prd-api-02"} and kafka["via"]
    worker = next(i for i in a["inbound"] if i["source"] == "worker-01")
    assert worker["via"] == ["db-01"] and worker["port"] == 5432
