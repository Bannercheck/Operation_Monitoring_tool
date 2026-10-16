"""Which technology each integration runs on (techstack): every evidence source, the confidence rules (a port alone never names a
framework), manual overrides with team scope, the agent's fingerprint dictionary on a fake /proc (only tag names leave the host),
flow.tech through the Python and Go parse paths, and the net_flows migration of an older table."""
import importlib.util
import json
import os
import random
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from watchover.db import Database
from watchover.flows import FlowStore, clean_tags
from watchover.live import LiveStore, simulate_flows
from watchover.techstack import TechStack, simulate_tech_lines

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)


def load_agent():
    spec = importlib.util.spec_from_file_location("wo_agent_ts", ROOT / "agent.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    return a


def flow(host, d, proc, ep, tech=None, n=3):
    return {"ts": datetime.now(UTC).isoformat(), "host": host, "msg": f"flow {d} {proc} {ep}", "kind": "flow", "flow.dir": d, "flow.process": proc,
            ("flow.remote" if d == "out" else "flow.port"): ep, "flow.count": n, **({"flow.tech": tech} if tech is not None else {})}


def jl(rows) -> bytes:
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode()


@pytest.fixture
def world(tmp_path):
    """A database with an inventory, the flow store with a TechStack, a live store that feeds both."""
    from watchover.inventory import Inventory
    db = Database(str(tmp_path / "k.db"))
    inv = Inventory(db)
    for h, ip, os_, app, role in (("api-01", "10.0.0.10", "Ubuntu 22.04 LTS", "Ödeme API", "API"), ("ora-01", "10.0.0.20", "Oracle Linux 8", "", "Oracle"),
                                  ("sql-01", "10.0.0.30", "Windows Server 2019", "SQL Server 2019", "Database"), ("web-01", "10.0.0.40", "", "", ""),
                                  ("sftp-01", "10.0.0.50", "", "", ""), ("gw-01", "10.0.0.60", "", "", ""), ("iis-01", "10.0.0.70", "", "", "")):
        inv.upsert({"hostname": h, "ip": ip, "os": os_, "application": app, "role": role})
    fs = FlowStore(db)
    ts = TechStack(db)
    live = LiveStore(spool=None)
    fs.tech = ts
    live.on_flows = fs.add
    from watchover.traces import TraceStore
    tr = TraceStore(db, settings=lambda: {"trace_sample_pct": 100})
    tr.on_spans = ts.observe_spans
    live.on_ingest = lambda ev: (ts.observe(ev), tr.add_events(ev))
    return db, fs, ts, live


def tech_of(fs, ts, host, target=None, port=None, side="outbound"):
    ts.flush(); ts.invalidate()
    m = fs.host_map(host, minutes=None)
    items = m[side]
    key = "target" if side == "outbound" else "source"
    return next(o for o in items if (target is None or o[key] == target) and (port is None or o["port"] == port))["tech"], m


# ---------------------------------------------------------------- evidence sources and confidence
def test_port_alone_never_names_a_framework(world):
    db, fs, ts, live = world
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "?", "10.0.0.10:8000")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["label"] == "HTTP API" and t["confidence"] == "olası" and t["protocol"] == {"name": "HTTP", "family": "api", "port": 8000, "tls": False, "key": "http"}
    assert t["server"]["framework"] is None and t["server"]["name"] is None and t["client"]["name"] is None       # unknown parts stay null
    assert [e["source"] for e in t["evidence"]] == ["port"] and "never names the framework" in t["evidence"][0]["en"]


def test_process_listener_and_agent_fingerprints(world):
    db, fs, ts, live = world
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.10:8000"), flow("api-01", "in", "uvicorn", "8000")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["client"]["runtime"] == "Java" and t["server"]["product"] == "uvicorn" and t["server"]["runtime"] == "Python"
    assert t["label"] == "HTTP · uvicorn" and t["confidence"] == "olası"                        # old agents: process names only, one source
    assert {e["source"] for e in t["evidence"]} == {"port", "process", "listener"}
    # a newer agent: the listener's fingerprints prove the framework; pydantic_core alone only makes it likely
    live.ingest("flows.jsonl", jl([flow("api-01", "in", "uvicorn", "8000", "pydantic_core,python,uvicorn")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["server"]["framework"] == "FastAPI" and t["label"] == "REST API · FastAPI" and t["confidence"] == "olası"
    live.ingest("flows.jsonl", jl([flow("api-01", "in", "uvicorn", "8000", "fastapi,pydantic_core,python,uvicorn"),
                                   flow("web-01", "out", "java", "10.0.0.10:8000", "java,spring-boot,cxf")]), "a")
    t, m = tech_of(fs, ts, "web-01")
    assert t["label"] == "REST API · FastAPI" and t["confidence"] == "kesin" and t["client"]["name"] == "Spring Boot"
    assert any(e["source"] == "agent" and "fastapi" in e["en"].lower() for e in t["evidence"])
    api = fs.host_map("api-01", minutes=None)
    assert [s["name"] for s in api["stack"] if s["layer"] == "app"] == ["FastAPI"]                 # starlette / helpers do not crowd the stack
    assert {s["layer"] for s in api["stack"]} >= {"app", "web", "runtime", "os"}
    assert api["listens"][0]["tech"]["label"] == "REST API · FastAPI"
    assert fs.hosts(minutes=None)[0]["tech"] and next(h for h in fs.hosts(minutes=None) if h["host"] == "api-01")["tech"][:2] == ["FastAPI", "uvicorn"]


def test_db_protocols_inventory_and_two_sources(world):
    db, fs, ts, live = world
    live.ingest("flows.jsonl", jl([flow("api-01", "out", "w3wp", "10.0.0.30:1433", "dotnet,iis,sqlclient"), flow("api-01", "out", "java", "10.0.0.20:1521"),
                                   flow("ora-01", "in", "tnslsnr", "1521")]), "a")
    t, _ = tech_of(fs, ts, "api-01", "sql-01")
    assert t["label"] == "TDS · SQL Server 2019 (ADO.NET SqlClient)" and t["server"]["version"] == "2019"
    assert t["confidence"] == "yüksek" and {e["source"] for e in t["evidence"]} >= {"port", "inventory", "agent"}   # port + inventory agree
    t, _ = tech_of(fs, ts, "api-01", "ora-01")
    assert t["label"] == "Oracle Net · Oracle Database" and t["confidence"] == "yüksek" and t["protocol"]["family"] == "db"
    assert {e["source"] for e in t["evidence"]} >= {"port", "listener", "inventory"}


def test_trace_evidence_is_direct(world):
    db, fs, ts, live = world
    live.ingest("flows.jsonl", jl([flow("api-01", "out", "python3", "10.0.0.20:1521", "oracledb,python")]), "a")
    t, _ = tech_of(fs, ts, "api-01")
    assert t["confidence"] == "yüksek"                                                            # port + inventory
    live.ingest("otel.jsonl", simulate_tech_lines(random.Random(1)).replace(b"pay-api-01", b"api-01"), "a")
    t, m = tech_of(fs, ts, "api-01")
    assert t["label"] == "Oracle Net · Oracle Database (python-oracledb)" and t["confidence"] == "kesin"
    assert any(e["source"] == "trace" and "db.system=oracle" in e["en"] for e in t["evidence"])
    assert ts.services("api-01") == ["payment-api"]                                               # recorded at ingest, no spans query
    assert not any(s["name"] == "Oracle Database" for s in m["stack"])                            # a peer's database is not this host's stack
    assert any(s["name"] == "FastAPI" and s["confidence"] == "kesin" for s in m["stack"])         # instrumentation scope


def test_log_evidence_family_file_and_logger(world):
    db, fs, ts, live = world
    lines = "\n".join(json.dumps({"ts": datetime.now(UTC).isoformat(), "host": "gw-01", "level": "info", "logger": "org.springframework.web.servlet.DispatcherServlet",
                                  "msg": f"request {i}"}) for i in range(3)) + "\n"
    live.ingest("app.jsonl", lines.encode(), "gw-01")
    live.ingest("catalina.out", b"15-Oct-2026 10:00:00.000 INFO [main] org.apache.catalina.startup.Catalina.start Server startup in 1200 ms\n", "gw-01")
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "node", "10.0.0.60:8080")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    srcs = {e["source"] for e in t["evidence"]}
    assert "log" in srcs and t["server"]["product"] == "Tomcat" and t["server"]["framework"] == "Spring Boot"
    assert t["label"] == "HTTP · Tomcat (Spring Boot)" and t["confidence"] == "olası"            # logs alone: one source
    rows = db._exec("SELECT k, v FROM tech_seen WHERE host='gw-01' AND source='log'")
    assert {(r["k"], r["v"]) for r in rows} >= {("logger", "org.springframework"), ("file", "catalina")}
    assert not any("DispatcherServlet" in r["v"] or r["v"].endswith(".out") for r in rows)       # only canonical tokens are stored


def test_banner_and_ssh_kind(world):
    db, fs, ts, live = world
    from watchover.scanner import ScanStore
    ScanStore(db)
    db._exec("INSERT INTO scan_results (hostname, ip, open_ports, scanned_at) VALUES (?,?,?,?)",
             ("sftp-01", "10.0.0.50", json.dumps([{"port": 22, "service": "ssh", "banner": "SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.6"}]), "2026-10-15T10:00:00+00:00"))
    live.ingest("flows.jsonl", jl([flow("api-01", "out", "ssh", "10.0.0.50:22", "sftp")]), "a")
    t, _ = tech_of(fs, ts, "api-01")
    assert t["label"] == "SFTP · OpenSSH 8.9p1" and t["confidence"] == "kesin" and t["server"]["version"] == "8.9p1"
    assert any(e["source"] == "banner" for e in t["evidence"])
    s = fs.host_map("sftp-01", minutes=None)["stack"]
    assert any(x["layer"] == "os" and x["name"] == "Ubuntu" for x in s) and any(x["name"] == "OpenSSH" and x["confidence"] == "kesin" for x in s)


def test_proxy_chain_and_demo_shapes(tmp_path):
    from watchover.inventory import Inventory
    from watchover.techstack import SIM_TECH_EXTRA_INVENTORY, SIM_TECH_INVENTORY
    db = Database(str(tmp_path / "k.db"))
    inv = Inventory(db)
    for h, ip, role in (("db-01", "172.16.1.55", "PostgreSQL"), ("cache-01", "172.16.1.60", "Redis"), ("kafka-01", "172.16.1.70", "Kafka"), ("hr-db", "172.16.2.10", "Oracle")):
        inv.upsert({"hostname": h, "ip": ip, "role": role, "application": SIM_TECH_EXTRA_INVENTORY.get(h, "")})
    for h, ip, os_, app, role in SIM_TECH_INVENTORY:
        inv.upsert({"hostname": h, "ip": ip, "os": os_, "application": app, "role": role})
    fs, ts, live = FlowStore(db), TechStack(db), LiveStore(spool=None)
    fs.tech = ts; live.on_flows = fs.add; live.on_ingest = ts.observe
    live.ingest("flows.jsonl", simulate_flows(random.Random(1)), "sim")
    live.ingest("hr-api.jsonl", jl([{"ts": datetime.now(UTC).isoformat(), "host": "hr-app-01", "service": "hr-api", "msg": "GET /x 200", "trace_id": "t1",
                                    "duration_ms": 5, "http.route": "/x", "span.kind": "server"}]), "hr-app-01")
    ts.flush(); ts.invalidate()                                  # REST routes elsewhere on hr-app-01 do not beat a SOAP stack on both ends of :7001
    labels = {}
    for h in ("crm-web-01", "erp-int-01", "web-iis-01", "worker-01", "batch-01", "prd-api-01"):
        for o in fs.host_map(h, minutes=None)["outbound"]:
            labels[(h, o["target"])] = o["tech"]["label"]
    assert labels[("crm-web-01", "portal-01")] == "REST API · nginx → uvicorn (FastAPI)"
    assert labels[("erp-int-01", "sap-ecc-01")] == "SAP RFC · SAP ECC 6.0 EHP8 (SAP JCo)"
    assert labels[("erp-int-01", "hr-app-01")] == "SOAP · WebLogic"
    assert labels[("erp-int-01", "kafka-01")] == "Kafka · Spring Kafka"
    assert labels[("web-iis-01", "sql-01")] == "TDS · SQL Server 2019 (ADO.NET SqlClient)"
    assert labels[("worker-01", "db-01")] == "PostgreSQL · psycopg / libpq" and labels[("worker-01", "mq-01")] == "AMQP · RabbitMQ 3.12"
    assert labels[("batch-01", "sftp-01")] == "SFTP · OpenSSH 8.9p1" and labels[("prd-api-01", "db-01")] == "PostgreSQL · PostgreSQL JDBC"
    chain = next(o for o in fs.host_map("crm-web-01", minutes=None)["outbound"])["tech"]["server"]["chain"]
    assert chain[0]["host"] == "pay-api-01" and chain[0]["port"] == 8000 and chain[0]["framework"] == "FastAPI" and chain[0]["runtime"] == "Python"
    # a reader who may not see the target gets no evidence from it (team isolation): the port only
    t = next(o for o in fs.host_map("crm-web-01", minutes=None, visible=lambda h: h == "crm-web-01")["outbound"])["tech"]
    assert t["label"] == "HTTPS API" and not any("portal-01" in e["en"] or "pay-api-01" in e["en"] for e in t["evidence"])


# ---------------------------------------------------------------- storage: tags, migration, old agents
def test_clean_tags_and_old_agents(world):
    db, fs, ts, live = world
    assert clean_tags("uvicorn, FastAPI ,python") == "fastapi,python,uvicorn"
    assert clean_tags(["uvicorn"]) == "" and clean_tags("rm -rf /,/etc/passwd,<b>x</b>,java") == "java" and clean_tags(None) == ""
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.10:8000", "java,spring-boot")]), "a")
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.10:8000")]), "a")                  # an old agent: no flow.tech
    r = db._exec("SELECT tech, samples FROM net_flows WHERE host='web-01'")[0]
    assert r["tech"] == "java,spring-boot" and r["samples"] == 2                                            # the last known tags are kept


def test_migrates_old_net_flows_table(tmp_path):
    db = Database(str(tmp_path / "old.db"))
    db._exec("""CREATE TABLE net_flows (host TEXT, process TEXT, dir TEXT, endpoint TEXT, env TEXT DEFAULT '', last_count INTEGER,
        samples INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, PRIMARY KEY (host, process, dir, endpoint))""")
    db._exec("INSERT INTO net_flows VALUES ('a','java','out','10.0.0.9:1521','',3,4,'2026-10-14T10:00:00+00:00','2026-10-15T10:00:00+00:00')")
    fs = FlowStore(db)
    assert "tech" in db.columns("net_flows")
    live = LiveStore(spool=None); live.on_flows = fs.add
    live.ingest("flows.jsonl", jl([flow("a", "out", "java", "10.0.0.9:1521", "java,ojdbc")]), "a")
    r = db._exec("SELECT * FROM net_flows WHERE host='a'")[0]
    assert r["tech"] == "java,ojdbc" and r["samples"] == 5 and r["first_seen"].startswith("2026-10-14")
    FlowStore(db)                                                                                           # idempotent
    fs2 = FlowStore(db); fs2.tech = TechStack(db)
    assert fs2.host_map("a", minutes=None)["outbound"][0]["tech"]["client"]["libs"] == ["Oracle JDBC"]


@pytest.mark.skipif(not os.environ.get("WATCHOVER_TEST_DATABASE_URL"), reason="WATCHOVER_TEST_DATABASE_URL not set")
def test_migrates_old_net_flows_table_postgres():
    db = Database(os.environ["WATCHOVER_TEST_DATABASE_URL"])
    for t in ("net_flows", "integ_tech", "tech_seen"):
        db._exec(f"DROP TABLE IF EXISTS {t}")
    db._exec("""CREATE TABLE net_flows (host TEXT, process TEXT, dir TEXT, endpoint TEXT, env TEXT DEFAULT '', last_count INTEGER,
        samples INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, PRIMARY KEY (host, process, dir, endpoint))""")
    fs = FlowStore(db); ts = TechStack(db); fs.tech = ts
    assert "tech" in db.columns("net_flows")
    live = LiveStore(spool=None); live.on_flows = fs.add; live.on_ingest = ts.observe
    live.ingest("flows.jsonl", jl([flow("a", "out", "java", "10.0.0.9:1521", "java,ojdbc")]), "a")
    live.ingest("flows.jsonl", jl([flow("a", "out", "java", "10.0.0.9:1521")]), "a")
    assert db._exec("SELECT tech FROM net_flows WHERE host='a'")[0]["tech"] == "java,ojdbc"
    live.ingest("otel.jsonl", simulate_tech_lines(random.Random(1)), "a"); ts.flush(); ts.flush()
    assert db._exec("SELECT COUNT(*) AS n FROM tech_seen")[0]["n"] > 0
    ts.set_override("a", "b", 443, None, "u_x", server="X"); ts.set_override("a", "b", 443, None, "u_x", server="Y")
    assert ts.overrides("a")[("a", "b", 443)]["server"] == "Y" and ts.delete_override("a", "b", 443, None) == 1


# ---------------------------------------------------------------- the agent: fingerprints on a fake /proc, only tags leave
def _hx(ip: str, port: int) -> str:
    return "".join(f"{int(x):02X}" for x in reversed(ip.split("."))) + f":{port:04X}"


def _proc(root: Path, pid: int, comm: str, cmdline: bytes, maps: list, inodes: list, start: str = "12345") -> None:
    d = root / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(cmdline)
    (d / "maps").write_text("".join(f"7f00{i:04x}000-7f00{i:04x}fff r-xp 00000000 08:01 {1000 + i} {p}\n" for i, p in enumerate(maps)) +
                            "7ffd00000000-7ffd00021000 rw-p 00000000 00:00 0 [stack]\n")
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(["S"] + ["0"] * 18 + [start] + ["0"] * 10) + "\n")
    for i, ino in enumerate(inodes):
        os.symlink(f"socket:[{ino}]", d / "fd" / str(3 + i))


@pytest.fixture
def fakeproc(tmp_path):
    root = tmp_path / "proc"
    (root / "net").mkdir(parents=True)
    me = "10.0.0.10"
    rows = [("0.0.0.0", 8000, "0.0.0.0", 0, "0A", 300), ("0.0.0.0", 443, "0.0.0.0", 0, "0A", 301), ("0.0.0.0", 22, "0.0.0.0", 0, "0A", 302),
            (me, 40000, "172.16.2.10", 1521, "01", 201), (me, 8000, "10.0.0.20", 50000, "01", 211), (me, 40001, "172.16.3.20", 3300, "01", 202),
            (me, 443, "10.0.0.30", 50001, "01", 203), (me, 22, "10.0.0.40", 50002, "01", 204)]
    lines = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"]
    for i, (lip, lp, rip, rp, st, ino) in enumerate(rows):
        lines.append(f"   {i}: {_hx(lip, lp)} {_hx(rip, rp)} {st} 00000000:00000000 00:00000000 00000000     0        0 {ino} 1")
    (root / "net" / "tcp").write_text("\n".join(lines) + "\n")
    _proc(root, 101, "python3", b"/usr/bin/python3\0/home/ayse/venv/bin/uvicorn\0main:app\0--env-file\0/home/ayse/.env\0--password=hunter2\0",
          ["/home/ayse/venv/lib/python3.11/site-packages/pydantic_core/_pydantic_core.cpython-311-x86_64-linux-gnu.so",
           "/home/ayse/venv/lib/python3.11/site-packages/uvloop/loop.cpython-311-x86_64-linux-gnu.so",
           "/home/ayse/venv/lib/python3.11/site-packages/oracledb/thin_impl.cpython-311-x86_64-linux-gnu.so", "/usr/lib/libpython3.11.so.1.0"], [201, 211])
    _proc(root, 102, "java", b"/usr/bin/java\0-Dspring.datasource.password=hunter2\0-cp\0/home/ayse/libs/ojdbc8.jar:/opt/sap/sapjco3.jar\0"
                             b"org.springframework.boot.loader.JarLauncher\0", ["/usr/lib/jvm/java-17/lib/server/libjvm.so"], [202])
    _proc(root, 103, "nginx", b"nginx: master process /usr/sbin/nginx -c /etc/nginx/nginx.conf", [], [203])
    _proc(root, 104, "sshd", b"sshd: ayse@pts/0", [], [204])
    return root


def test_agent_fingerprints_only_tags_leave(fakeproc, monkeypatch):
    agent = load_agent()
    monkeypatch.setattr(agent, "PROC", str(fakeproc))
    fl = agent.tcp_flows()
    by = {(f["flow.dir"], f["flow.process"], f.get("flow.remote") or f.get("flow.port")): f for f in fl}
    assert set(by) == {("out", "python3", "172.16.2.10:1521"), ("in", "python3", "8000"), ("out", "java", "172.16.3.20:3300"), ("in", "nginx", "443"),
                       ("in", "sshd", "22")}
    assert by[("out", "python3", "172.16.2.10:1521")]["flow.tech"] == "oracledb,pydantic_core,python,uvicorn,uvloop"
    assert by[("in", "python3", "8000")]["flow.tech"] == by[("out", "python3", "172.16.2.10:1521")]["flow.tech"]
    assert by[("out", "java", "172.16.3.20:3300")]["flow.tech"] == "java,ojdbc,sapjco,spring-boot"
    assert by[("in", "nginx", "443")]["flow.tech"] == "nginx" and by[("in", "sshd", "22")]["flow.tech"] == "openssh"
    assert all(isinstance(f["flow.tech"], str) for f in fl)                                                # one string, never a list
    dump = json.dumps(fl)
    for secret in ("hunter2", "ayse", "/home", "/usr", "password", ".env", "main:app", "pts/0", "nginx.conf", ".so", ".jar"):
        assert secret not in dump, secret
    known = {t for t, _, _ in agent.FINGERPRINTS}
    assert all(set(f["flow.tech"].split(",")) <= known for f in fl)
    # read once per process: a changed command line is not re-read until the pid is a new process (start time)
    (fakeproc / "101" / "cmdline").write_bytes(b"/usr/bin/python3\0/opt/gunicorn\0")
    assert {f["flow.tech"] for f in agent.tcp_flows() if f["flow.process"] == "python3"} == {"oracledb,pydantic_core,python,uvicorn,uvloop"}
    (fakeproc / "101" / "stat").write_text("101 (python3) " + " ".join(["S"] + ["0"] * 18 + ["99999"] + ["0"] * 10) + "\n")
    assert {f["flow.tech"] for f in agent.tcp_flows() if f["flow.process"] == "python3"} == {"gunicorn,oracledb,pydantic_core,python,uvloop"}


def test_agent_dictionary_matches_server_catalogue():
    from watchover.techstack import HELPER_TAGS, TECH
    agent = load_agent()
    tags = {t for t, _, _ in agent.FINGERPRINTS}
    assert tags <= set(TECH) | HELPER_TAGS
    assert all(w in ("arg0", "prog", "tok", "cmd", "map") and nd == nd.lower() for _, w, nd in agent.FINGERPRINTS)
    assert {"uvicorn", "gunicorn", "fastapi", "django", "flask", "celery", "pydantic_core", "spring-boot", "tomcat", "weblogic", "jboss", "kafka-broker",
            "elasticsearch", "ojdbc", "oci", "libpq", "mysqlclient", "msodbcsql", "sapnwrfc", "sapjco", "librdkafka", "node", "dotnet", "php"} <= tags


# ---------------------------------------------------------------- flow.tech through both parse paths
TECH_LINES = jl([flow("h1", "out", "python3", "10.0.0.9:1521", "oracledb,python,uvicorn"), flow("h1", "in", "python3", "8000", "fastapi,uvicorn"),
                 flow("h2", "out", "java", "10.0.0.9:1521")])


def _flows_of(store_ingest) -> list:
    got = []
    s = LiveStore(spool=None)
    s.on_flows = lambda fl: got.extend(fl)
    store_ingest(s)
    return [(o.host, o.attributes.get("flow.dir"), o.attributes.get("flow.tech")) for o in got]


def test_python_and_v2_records_keep_flow_tech(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "h"))
    from watchover.pipeline import ingest_bytes
    a = _flows_of(lambda s: s.ingest("flows.jsonl", TECH_LINES, "lt"))
    assert a == [("h1", "out", "oracledb,python,uvicorn"), ("h1", "in", "fastapi,uvicorn"), ("h2", "out", None)]
    obs, rep = ingest_bytes("flows.jsonl", TECH_LINES)
    body = "".join(json.dumps({"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity, "service": o.service,
                               "host": o.host, "environment": o.environment, "origin": o.origin, "attributes": o.attributes, "source": o.source,
                               "line_no": o.line_no, "parser": o.parser, "parser_confidence": o.parser_confidence, "raw": o.raw}, default=str) + "\n" for o in obs).encode()
    h = {"v": 2, "kind": "parsed", "file": "flows.jsonl", "agent": "lt", "format": rep[0]["format"], "family": rep[0].get("family", ""),
         "confidence": rep[0].get("confidence", 0.9), "hint_format": "jsonl", "report": {k: v for k, v in rep[0].items() if k not in ("sha", "table")}}
    assert _flows_of(lambda s: s.ingest_parsed(body, h)) == a


@pytest.fixture(scope="module")
def go_binary(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    out = tmp_path_factory.mktemp("bin") / "wo-receiver"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "receiver", check=True, capture_output=True, timeout=600)
    return out


def test_go_parse_stage_keeps_flow_tech(go_binary, monkeypatch, tmp_path):
    from watchover.spool_consumer import SpoolConsumer
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "h"))
    d = tmp_path / "spool"; d.mkdir()
    hdr = {"v": 1, "path": "/ingest", "file": "flows.jsonl", "ip": "10.0.0.9", "ts": "2026-10-15T12:00:00Z", "len": len(TECH_LINES), "identity": "token",
           "agent": "lt", "agent_id": 7, "env": "prod", "site": "ist"}
    (d / "seg-0000000000000000001.raw").write_bytes(json.dumps(hdr, separators=(",", ":")).encode() + b"\n" + TECH_LINES + b"\n")
    r = subprocess.run([str(go_binary), "parse-spool", str(d)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["parse_batches_parsed"] == 1
    got = []
    s = LiveStore(spool=None); s.on_flows = lambda fl: got.extend(fl)
    SpoolConsumer(str(d), s).run_once()
    assert [(o.host, o.attributes.get("flow.dir"), o.attributes.get("flow.tech")) for o in got] == \
        [("h1", "out", "oracledb,python,uvicorn"), ("h1", "in", "fastapi,uvicorn"), ("h2", "out", None)]


# ---------------------------------------------------------------- manual override: API, permission, team scope
@pytest.fixture
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0",
                 "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    for email, role in (("ea@corp.com", "operator"), ("hr@corp.com", "operator"), ("view@corp.com", "viewer")):
        svc.users.register(email, "Str0ng!Passw0rd", role=role)
    with TestClient(a) as c:
        c.svc = svc
        yield c


def login(c, email, pw="Str0ng!Passw0rd"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_manual_override_and_team_scope(app):
    c, svc = app, app.svc
    for h, ip, app_ in (("ea-app-01", "10.10.0.5", "EA portal"), ("hr-app-01", "10.30.0.5", "HR Bordro (IIS 10)"), ("fin-01", "10.40.0.9", "Finance")):
        svc.inventory.upsert({"hostname": h, "ip": ip, "application": app_, "env": "prod"})
    svc.live.ingest("flows.jsonl", jl([flow("ea-app-01", "out", "java", "10.30.0.5:443", "java,spring-boot"), flow("hr-app-01", "out", "w3wp", "10.40.0.9:1433")]), "agent")
    admin = login(c, "admin@watchover.local", "Test-Yonetici-Parola-42")
    for name, glob, member in (("EA", "ea-*", "ea@corp.com"), ("HR", "hr-*", "hr@corp.com")):
        r = c.post("/api/teams", json={"name": name, "rules": [{"field": "host", "op": "glob", "value": glob}], "members": [member]}, headers=admin)
        assert r.status_code in (200, 201), r.text
    ea, hr, viewer = login(c, "ea@corp.com"), login(c, "hr@corp.com"), login(c, "view@corp.com")
    svc.techstack.invalidate()
    out = lambda h, host: c.get(f"/api/apm/hosts/{host}/integrations?minutes=0", headers=h).json()   # noqa: E731
    # evidence about another team's host never reaches the reader: EA sees the port, the admin sees hr-app-01's inventory
    t_ea = out(ea, "ea-app-01")["outbound"][0]["tech"]
    t_ad = out(admin, "ea-app-01")["outbound"][0]["tech"]
    assert t_ea["label"] == "HTTPS API" and not any("hr-app-01" in e["en"] for e in t_ea["evidence"])
    assert t_ad["label"] == "HTTPS · IIS 10" and any(e["source"] == "inventory" for e in t_ad["evidence"])
    body = {"host": "ea-app-01", "target": "hr-app-01", "port": 443, "protocol": "REST API", "server": "Bordro API (.NET 8)", "note": "kontrat v2, şifre=gizli123"}
    assert c.put("/api/apm/integrations/tech", json=body, headers=viewer).status_code == 403                 # no act.inventory
    assert c.put("/api/apm/integrations/tech", json={**body, "host": "fin-01", "target": "hr-app-02"}, headers=ea).status_code == 404   # not EA's integration
    r = c.put("/api/apm/integrations/tech", json=body, headers=ea)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["team_id"] is not None and row["updated_by"].startswith("u") and "ea@corp.com" not in json.dumps(row) and "gizli123" not in row["note"]
    t_ea = out(ea, "ea-app-01")["outbound"][0]["tech"]
    assert t_ea["manual"] and t_ea["label"] == "REST API · Bordro API (.NET 8)" and t_ea["confidence"] == "kesin" and t_ea["evidence"][0]["source"] == "manual"
    t_hr = next(i for i in out(hr, "hr-app-01")["inbound"] if i["source"] == "ea-app-01")["tech"]
    assert not t_hr["manual"]                                                                             # another team's row stays with that team
    assert t_hr["client"]["name"] == "Java" and not any(e["source"] == "agent" for e in t_hr["evidence"])   # nor its host's fingerprints
    assert out(admin, "ea-app-01")["outbound"][0]["tech"]["manual"]                                      # installation-wide readers see team rows
    teams = {t["name"]: t["id"] for t in svc.teams.list()}
    assert svc.teams.owned(teams["EA"]).get("integ_tech") == 1
    q = "/api/apm/integrations/tech?host=ea-app-01&target=hr-app-01&port=443"
    assert c.delete(q, headers=hr).status_code == 404                                                     # HR has no row of its own there
    assert c.delete(q, headers=ea).json() == {"ok": True, "deleted": 1}
    assert not out(ea, "ea-app-01")["outbound"][0]["tech"]["manual"]
    hosts = c.get("/api/apm/hosts?minutes=0", headers=ea).json()
    assert {h["host"] for h in hosts} == {"ea-app-01"} and "tech" in hosts[0]


# ---------------------------------------------------------------- adversarial review: detection accuracy and privacy
def test_agent_fingerprints_name_the_program_not_its_arguments(tmp_path, monkeypatch):
    """`pip install fastapi` is not FastAPI, a Keycloak client is not Keycloak, Kafka tools are not a broker; process titles
    (gunicorn: worker) and SAP's renamed binaries (dw.sap<SID>_<inst>) are still recognised."""
    agent = load_agent()
    root = tmp_path / "proc"
    monkeypatch.setattr(agent, "PROC", str(root))
    cases = {
        201: ("python3", b"/usr/bin/python3\0-m\0pip\0install\0fastapi\0flask\0celery\0uvicorn\0gunicorn\0--index-url\0https://ayse:hunter2@pypi\0", "python"),
        202: ("java", b"/usr/bin/java\0-Dkeycloak.auth-server-url=https://sso\0-cp\0/opt/app/app.jar\0com.corp.Main\0", "java"),
        203: ("gunicorn: worke", b"gunicorn: worker [shop.wsgi:application]\0\0\0", "gunicorn"),
        204: ("dw.sapPRD_DVEBM", b"dw.sapPRD_DVEBMGS00\0pf=/usr/sap/PRD/SYS/profile/PRD_DVEBMGS00_sap01\0", "sap-abap"),
        205: ("python3", b"/venv/bin/python3\0/venv/bin/gunicorn\0-k\0uvicorn.workers.UvicornWorker\0main:app\0", "gunicorn,python,uvicorn"),
        206: ("java", b"/usr/bin/java\0-cp\0/opt/kafka/libs/kafka-clients-3.6.jar\0kafka.tools.ConsoleConsumer\0", "java,kafka-clients"),
        207: ("java", b"/usr/bin/java\0-Xmx1g\0-cp\0/opt/kafka/libs/*\0kafka.Kafka\0config/server.properties\0", "java,kafka-broker"),
        208: ("python3", b"/usr/bin/python3\0-W\0ignore\0manage.py\0runserver\0", "django,python"),
        209: ("java", b"/usr/bin/java\0-Dkc.home.dir=/opt/keycloak\0-cp\0/opt/keycloak/lib/quarkus-run.jar\0io.quarkus.bootstrap.runner.QuarkusEntryPoint\0",
              "java,keycloak"),
    }
    for pid, (comm, cmd, _) in cases.items():
        _proc(root, pid, comm, cmd, [], [])
    got = {pid: agent.fingerprint(pid) for pid in cases}
    assert got == {pid: want for pid, (_, _, want) in cases.items()}
    assert "hunter2" not in json.dumps(got) and "ayse" not in json.dumps(got)
    from watchover.techstack import proc_key
    assert proc_key("gunicorn: worke") == "gunicorn" and proc_key("dw.sapPRD_DVEBM") == "sap-abap" and proc_key("gw.sapPRD_DVEBM") == "sap-gateway"


def test_ambiguous_libraries_and_superseded_frameworks_never_win(world):
    db, fs, ts, live = world
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.10:8000"),
                                   flow("api-01", "in", "python3", "8000", "aiohttp,fastapi,pydantic_core,python,starlette,uvicorn")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["label"] == "REST API · FastAPI" and t["server"]["framework"] == "FastAPI"          # aiohttp (an HTTP client too) / starlette do not win
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.60:8000"), flow("gw-01", "in", "python3", "8000", "aiohttp,python")]), "a")
    t, _ = tech_of(fs, ts, "web-01", "gw-01")
    assert t["server"]["framework"] == "aiohttp" and t["confidence"] == "olası"                   # a loaded library alone is not proof
    assert any("not proof on its own" in e["en"] for e in t["evidence"])
    # Django and Flask loggers on one host, gunicorn serving the port: equally likely, so neither is named
    lines = "\n".join(json.dumps({"ts": datetime.now(UTC).isoformat(), "host": "sql-01", "level": "info", "logger": lg, "msg": "x"})
                      for lg in ("django.request", "flask.app")) + "\n"
    live.ingest("app.jsonl", lines.encode(), "sql-01")
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.30:8001"), flow("sql-01", "in", "gunicorn", "8001", "gunicorn,python")]), "a")
    t, _ = tech_of(fs, ts, "web-01", "sql-01")
    assert t["label"] == "HTTP · gunicorn" and t["server"]["framework"] is None
    # a SOAP-capable library on the client alone (CXF also does REST) does not make every HTTP call SOAP
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.70:8443", "cxf,java,spring-boot")]), "a")
    t, _ = tech_of(fs, ts, "web-01", "iis-01")
    assert not t["label"].startswith("SOAP")


def test_conflicting_direct_evidence_is_shown_and_not_kesin(world):
    db, fs, ts, live = world
    from watchover.scanner import ScanStore
    ScanStore(db)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db._exec("INSERT INTO scan_results (hostname, ip, open_ports, scanned_at) VALUES (?,?,?,?)",
             ("iis-01", "10.0.0.70", json.dumps([{"port": 443, "service": "https", "banner": "HTTP/1.1 200 OK Server: Microsoft-IIS/10.0"}]), now))
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.70:443"), flow("iis-01", "in", "nginx", "443", "nginx")]), "a")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["server"]["product"] == "nginx" and t["confidence"] == "yüksek"                      # the agent sees nginx now; the banner disagrees
    c = [e for e in t["evidence"] if e.get("conflict")]
    assert len(c) == 1 and "Microsoft-IIS" in c[0]["en"] and "chosen: nginx" in c[0]["en"]
    # the same banner, with nothing contradicting it, is direct proof
    db._exec("DELETE FROM net_flows WHERE host='iis-01'")
    t, _ = tech_of(fs, ts, "web-01")
    assert t["label"] == "HTTPS · IIS 10.0" and t["confidence"] == "kesin" and not any(e.get("conflict") for e in t["evidence"])


def test_stale_evidence_does_not_count(world):
    db, fs, ts, live = world
    from datetime import timedelta
    from watchover.scanner import ScanStore
    ScanStore(db)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    for k, v in (("scope", "opentelemetry.instrumentation.fastapi"), ("telemetry.sdk.language", "python"), ("http.route", "1")):
        db._exec("INSERT INTO tech_seen (host, source, k, v, n, last_seen) VALUES (?,?,?,?,?,?)", ("gw-01", "trace", k, v, 5, old))
    db._exec("INSERT INTO scan_results (hostname, ip, open_ports, scanned_at) VALUES (?,?,?,?)",
             ("sftp-01", "10.0.0.50", json.dumps([{"port": 22, "banner": "SSH-2.0-OpenSSH_7.4"}]), (datetime.now(UTC) - timedelta(days=200)).isoformat()))
    live.ingest("flows.jsonl", jl([flow("web-01", "out", "java", "10.0.0.60:8000"), flow("web-01", "out", "java", "10.0.0.50:22"),
                                   flow("gw-01", "in", "uvicorn", "8000", "fastapi,python,uvicorn", n=1)]), "a")
    db._exec("UPDATE net_flows SET last_seen=? WHERE host='gw-01'", ((datetime.now(UTC) - timedelta(days=3)).isoformat(),))
    ts.flush(); ts.invalidate()
    m = fs.host_map("web-01", minutes=1440)
    t = next(o for o in m["outbound"] if o["port"] == 8000)["tech"]
    assert t["label"] == "HTTP API" and t["server"]["framework"] is None and t["confidence"] == "olası"   # 10-day-old traces, 3-day-old listener
    t = next(o for o in m["outbound"] if o["port"] == 22)["tech"]
    assert t["server"]["product"] == "OpenSSH" and t["confidence"] != "kesin" and any("old scan" in e["en"] for e in t["evidence"])
    t = next(o for o in fs.host_map("web-01", minutes=None)["outbound"] if o["port"] == 8000)["tech"]
    assert t["label"] == "REST API · FastAPI" and t["confidence"] == "kesin"                              # no window: all that is kept


def test_malicious_flow_tech_is_dropped(world):
    db, fs, ts, live = world
    assert clean_tags("<script>alert(1)</script>,java,ayse.yilmaz,alice,123,uvicorn,../../etc/passwd") == "java,uvicorn"
    assert clean_tags("x," * 2_000_000 + "java") == "" and clean_tags(",".join(["java"] * 100000)) == "java"
    bad = [flow("web-01", "out", "java", "10.0.0.10:8000", "<img src=x onerror=alert(1)>,ayse,uvicorn"),
           {**flow("web-01", "out", "node", "10.0.0.10:8001"), "flow.tech": ["java", "uvicorn"]},
           {**flow("web-01", "out", "php", "10.0.0.10:8002"), "flow.tech": 1234},
           {**flow("web-01", "out", "perl", "10.0.0.10:8003"), "flow.tech": {"a": "fastapi"}},
           flow("web-01", "out", "ruby", "10.0.0.10:8004", "A" * 300000 + ",fastapi")]
    live.ingest("flows.jsonl", jl(bad), "a")
    assert {r["process"]: r["tech"] for r in db._exec("SELECT process, tech FROM net_flows WHERE host='web-01'")} == \
        {"java": "uvicorn", "node": "", "php": "", "perl": "", "ruby": ""}
    dump = json.dumps(fs.host_map("web-01", minutes=None))
    assert "<img" not in dump and "ayse" not in dump and "onerror" not in dump


ADVERSARIAL = jl([flow("h1", "out", "java", "10.0.0.9:1521", "<img src=x onerror=alert(1)>,java,ojdbc,ayse"),
                  {**flow("h1", "out", "node", "10.0.0.9:8001"), "flow.tech": ["java", "uvicorn"]},
                  {**flow("h1", "out", "php", "10.0.0.9:8002"), "flow.tech": 1234},
                  {**flow("h1", "out", "perl", "10.0.0.9:8003"), "flow.tech": {"a": "fastapi"}},
                  {**flow("h1", "out", "lua", "10.0.0.9:8004"), "flow.tech": True},
                  flow("h1", "out", "ruby", "10.0.0.9:8005", "B" * 5000 + ",fastapi"),
                  flow("h1", "out", "python3", "10.0.0.9:8006", " FastAPI , Uvicorn,python ")])


def _stored(tmp_path, name, feed) -> dict:
    db = Database(str(tmp_path / f"{name}.db"))
    fs = FlowStore(db)
    s = LiveStore(spool=None)
    s.on_flows = fs.add
    feed(s)
    return {(r["process"], r["endpoint"]): r["tech"] for r in db._exec("SELECT process, endpoint, tech FROM net_flows")}


def test_go_and_python_store_the_same_flow_tech(go_binary, monkeypatch, tmp_path):
    """Hostile flow.tech values (markup, lists, numbers, objects, booleans, huge strings) end up identical through the Python
    ingest and the Go parse stage + spool consumer: known tags only."""
    from watchover.spool_consumer import SpoolConsumer
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "h"))
    py = _stored(tmp_path, "py", lambda s: s.ingest("flows.jsonl", ADVERSARIAL, "lt"))
    d = tmp_path / "spool"; d.mkdir()
    hdr = {"v": 1, "path": "/ingest", "file": "flows.jsonl", "ip": "10.0.0.9", "ts": "2026-10-15T12:00:00Z", "len": len(ADVERSARIAL), "identity": "token",
           "agent": "lt", "agent_id": 7, "env": "prod", "site": "ist"}
    (d / "seg-0000000000000000001.raw").write_bytes(json.dumps(hdr, separators=(",", ":")).encode() + b"\n" + ADVERSARIAL + b"\n")
    r = subprocess.run([str(go_binary), "parse-spool", str(d)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    go = _stored(tmp_path, "go", lambda s: SpoolConsumer(str(d), s).run_once())
    assert py == go
    assert py[("java", "10.0.0.9:1521")] == "java,ojdbc" and py[("python3", "10.0.0.9:8006")] == "fastapi,python,uvicorn"
    assert all(v in ("", "java,ojdbc", "fastapi,python,uvicorn") for v in py.values())


def test_team_reader_gets_404_and_admin_sees_installation_row(app):
    c, svc = app, app.svc
    for h, ip in (("ea-app-01", "10.10.0.5"), ("hr-app-01", "10.30.0.5")):
        svc.inventory.upsert({"hostname": h, "ip": ip, "env": "prod"})
    svc.live.ingest("flows.jsonl", jl([flow("ea-app-01", "out", "java", "10.30.0.5:443", "java,spring-boot"),
                                       flow("hr-app-01", "in", "w3wp", "443", "aspnet,dotnet,iis")]), "agent")
    admin = login(c, "admin@watchover.local", "Test-Yonetici-Parola-42")
    ids = {}
    for name, glob, member in (("EA", "ea-*", "ea@corp.com"), ("HR", "hr-*", "hr@corp.com")):
        r = c.post("/api/teams", json={"name": name, "rules": [{"field": "host", "op": "glob", "value": glob}], "members": [member]}, headers=admin)
        assert r.status_code in (200, 201), r.text
    ids = {t["name"]: t["id"] for t in svc.teams.list()}
    ea, hr = login(c, "ea@corp.com"), login(c, "hr@corp.com")
    svc.techstack.invalidate()
    assert c.get("/api/apm/hosts/hr-app-01/integrations?minutes=0", headers=ea).status_code == 404       # another team's host: nothing
    hosts = c.get("/api/apm/hosts?minutes=0", headers=ea).json()
    assert {h["host"] for h in hosts} == {"ea-app-01"} and "IIS" not in json.dumps(hosts)
    t = c.get("/api/apm/hosts/ea-app-01/integrations?minutes=0", headers=ea).json()["outbound"][0]["tech"]
    assert "IIS" not in json.dumps(t) and "w3wp" not in json.dumps(t)                                    # not even through the label
    body = {"host": "ea-app-01", "target": "hr-app-01", "port": 443, "server": "X"}
    assert c.put("/api/apm/integrations/tech", json={**body, "team_id": ids["EA"]}, headers=hr).status_code == 403   # not HR's team row
    assert c.put("/api/apm/integrations/tech", json={**body, "server": "team row"}, headers=ea).status_code == 200
    assert c.put("/api/apm/integrations/tech", json={**body, "server": "installation row"}, headers=admin).status_code == 200
    q = lambda h: c.get("/api/apm/hosts/ea-app-01/integrations?minutes=0", headers=h).json()["outbound"][0]["tech"]   # noqa: E731
    assert q(admin)["server"]["name"] == "installation row" and q(admin)["team_id"] is None              # the admin's own row first
    assert q(ea)["server"]["name"] == "team row" and q(ea)["team_id"] == ids["EA"]
    assert c.delete("/api/apm/integrations/tech?host=ea-app-01&target=hr-app-01&port=443", headers=admin).json()["deleted"] == 1
    assert q(admin)["server"]["name"] == "team row"                                                       # then the newest team row
