"""wo-agent's log-shipping role (goagent/internal/ship, `wo-agent ship`) end to end and against agent.py:
- through the real Go receiver, the spool consumer and the LiveStore: every line once, across a restart, a rename rotation
  and a copytruncate, with metrics, under the token's identity;
- byte-for-byte the same bodies and headers as agent.py for the same files (universal newlines, invalid UTF-8, fragments,
  rotation), the same metrics / flows JSON on the same /proc, the same discovery profiles and fingerprint dictionary;
- the installer: argument checks, and the migration of an agent.py install to wo-agent in a sandbox root (no system change).
Skipped without a Go toolchain, like tests/test_go_receiver.py."""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "")
sys.path.insert(0, str(ROOT))
import agent as pyagent  # noqa: E402


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def clean_env(**kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WATCHOVER_", "AGENT_ENV", "WO_"))}
    env.update(kw)
    return env


@pytest.fixture(scope="module")
def wo_agent(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    d = tmp_path_factory.mktemp("woagent")
    subprocess.run([GO, "build", "-o", str(d / "wo-agent"), "."], cwd=ROOT / "goagent", check=True, capture_output=True, timeout=300,
                   env=dict(os.environ, CGO_ENABLED="0"))
    if ARCH:
        shutil.copy(d / "wo-agent", d / f"wo-agent-linux-{ARCH}")
    return d / "wo-agent"


@pytest.fixture(scope="module")
def receiver_bin(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    out = tmp_path_factory.mktemp("recv") / "wo-receiver"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "receiver", check=True, capture_output=True, timeout=300)
    return out


def append(p: Path, data: bytes):
    with open(p, "ab") as f:
        f.write(data)


def jl(tag, i):
    return (json.dumps({"level": "error" if i % 7 == 0 else "info", "service": "erp", "msg": f"{tag} {i}"}) + "\n").encode()


# ---------------------------------------------------------------- the whole path: wo-agent -> Go receiver -> spool -> LiveStore
def test_wo_agent_through_go_receiver_every_line_once(wo_agent, receiver_bin, tmp_path):
    from watchover.agents import AgentRegistry
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore, start_receiver
    from watchover.spool_consumer import SpoolConsumer
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    _rec, tok = reg.enroll("erp-app-01", env="prod", site="ist")
    store = LiveStore(spool=str(tmp_path / "live.jsonl"))
    spool = tmp_path / "rspool"
    cons = SpoolConsumer(str(spool), store, reg, key_fn=lambda: "")
    cons.export_auth(force=True)
    up_port, port = free_port(), free_port()
    upstream = start_receiver(store, up_port, None, reg)
    recv = subprocess.Popen([str(receiver_bin)], env=dict(os.environ, WO_LISTEN=f"127.0.0.1:{port}", WO_SPOOL=str(spool), WO_UPSTREAM=f"http://127.0.0.1:{up_port}",
                                                         WO_AGENT_DIR=str(tmp_path), WO_SEGMENT_MS="100"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logs = tmp_path / "logs"; logs.mkdir()
    app = logs / "app.log"
    append(app, b"".join(jl("old", i) for i in range(50)))                                   # before the first start: never shipped
    agent_dir = tmp_path / "agent"
    cmd = [str(wo_agent), "ship", "--url", f"http://127.0.0.1:{port}/ingest", "--token", tok, "--agent", "ignored-name", "--tail", str(logs / "*.log"),
           "--metrics", "--interval", "1", "--spool", str(agent_dir / "spool")]
    env = clean_env()
    want = []

    def write(tag, n):
        for i in range(n):
            append(app, jl(tag, i)); want.append(f"{tag} {i}")

    tags = ("old", "first", "second", "late-in-rotated", "while-stopped", "after-truncate")

    def shipped():
        cons.run_once()
        return [o.message for o in store.snapshot() if (o.message or "").split(" ")[0] in tags]

    try:
        assert wait(lambda: _ok(f"http://127.0.0.1:{port}/health"), 10)
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1.0)
        write("first", 200)
        time.sleep(1.5)
        os.rename(app, logs / "app.log.1")                                                   # rename rotation (app.log.1 is outside the glob)
        append(logs / "app.log.1", jl("late-in-rotated", 0)); want.append("late-in-rotated 0")
        write("second", 100)
        time.sleep(2.0)
        proc.terminate(); out = proc.communicate(timeout=30)[0].decode()                    # clean stop: positions saved
        assert proc.returncode == 0, out
        write("while-stopped", 100)
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1.0)
        data = app.read_bytes(); (logs / "app.log.2").write_bytes(data); os.truncate(app, 0)  # copytruncate
        write("after-truncate", 50)
        time.sleep(2.5)
        got = wait(lambda: (lambda s: s if len(s) >= len(want) else None)(shipped()), 20) or shipped()
        proc.terminate(); proc.communicate(timeout=30)
        cons.run_once()
        got = shipped()
        assert sorted(got) == sorted(want), (len(got), len(want), sorted(set(want) - set(got))[:5], [m for m in got if got.count(m) > 1][:5])
        assert not [m for m in got if m.startswith("old ")]
        evs = [o for o in store.snapshot() if o.message in set(want)]
        assert {o.attributes.get("agent") for o in evs} == {"erp-app-01"} and {o.environment for o in evs} == {"prod"}   # the token's identity
        names = {r[2] for r in store.metrics}
        assert {"cpu", "memory", "disk"} <= names, names
        pos = json.loads((agent_dir / "positions.json").read_text())
        assert any(f["path"] == str(app) and f["off"] == app.stat().st_size for f in pos["files"]), pos
    finally:
        recv.terminate(); recv.wait(10); upstream.shutdown()


def _ok(url):
    import urllib.request
    try:
        return urllib.request.urlopen(url, timeout=1).status == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- byte-for-byte parity with agent.py
class Recorder(BaseHTTPRequestHandler):
    got: list = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.lock:
            self.got.append({"name": self.headers.get("X-File-Name"), "agent": self.headers.get("X-Agent"), "auth": self.headers.get("Authorization"),
                             "ctype": self.headers.get("Content-Type"), "version": self.headers.get("X-Agent-Version"), "body": body})
        out = json.dumps({"accepted": body.count(b"\n"), "agent": "a", "identity": "token", "queued": True}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


def recorder():
    class R(Recorder):
        got = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, R


def tricky_session(d: Path, both_running):
    """The same writes for both agents: CRLF, lone CR, invalid UTF-8, a fragment completed later, a fragment left in a rotated file."""
    log = d / "app.log"
    log.write_bytes(b"existing line is not shipped\n")
    both_running(start=True)
    time.sleep(1.2)
    for chunk in [b"plain\n", b"crlf\r\nlone cr\rnext\n", b"bad \xff\xe2\x82 utf8 \xc3\xa7\n", b"frag", b"ment done\n", "türkçe \U0001F600\n".encode()]:
        append(log, chunk); time.sleep(0.3)
    append(log, b"last line before the rotation\n")
    time.sleep(1.2)
    os.rename(log, d / "app.log.1")
    append(log, b"new file 1\nnew file 2\n")
    time.sleep(3.5)
    both_running(start=False)


def test_tail_bodies_and_headers_match_agent_py(wo_agent, tmp_path):
    srv, R = recorder()
    port = srv.server_address[1]
    dpy, dgo = tmp_path / "py", tmp_path / "go"; dpy.mkdir(); dgo.mkdir()
    procs = {}

    def run(start):
        if start:
            url = f"http://127.0.0.1:{port}/ingest"
            procs["py"] = subprocess.Popen([sys.executable, str(ROOT / "agent.py"), "--url", url, "--token", "wo_t", "--agent", "py", "--tail", str(dpy / "app.log")],
                                           env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs["go"] = subprocess.Popen([str(wo_agent), "--url", url, "--token", "wo_t", "--agent", "go", "--tail", str(dgo / "app.log")],
                                           env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            for p in procs.values():
                p.terminate(); p.wait(15)

    # both agents follow their own copy of the same writes, concurrently
    t = threading.Thread(target=tricky_session, args=(dgo, lambda start: None)); t.start()
    tricky_session(dpy, run); t.join()
    srv.shutdown()
    by = {"py": [], "go": []}
    for g in R.got:
        by[g["agent"]].append(g)
    tails = {a: b"".join(g["body"] for g in v if g["name"] == "app.log") for a, v in by.items()}
    assert tails["py"] == tails["go"], (tails["py"], tails["go"])
    assert b"existing line" not in tails["go"] and tails["go"].endswith(b"last line before the rotation\nnew file 1\nnew file 2\n")
    hello = {a: json.loads(next(g["body"] for g in v if g["name"] == "agent.jsonl")) for a, v in by.items()}
    assert list(hello["py"]) == list(hello["go"]) == ["ts", "level", "service", "host", "env", "msg"]
    for a in ("py", "go"):
        g = by[a][0]
        assert g["auth"] == "Bearer wo_t" and g["ctype"] == "application/octet-stream" and g["version"]
    assert by["py"][0]["version"] == pyagent.__version__ and by["go"][0]["version"] != pyagent.__version__


def fake_proc(d: Path) -> Path:
    p = d / "proc"
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = ["   0: 00000000:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 100 1",
            "   1: 0A00000A:01BB 0B00000A:C350 01 00000000:00000000 00:00000000 00000000     0        0 101 1",
            "   2: 0A00000A:C351 0C00000A:05F1 01 00000000:00000000 00:00000000 00000000     0        0 102 1",
            "   3: 0A00000A:C352 0C00000A:05F1 01 00000000:00000000 00:00000000 00000000     0        0 103 1",
            "   4: 0A00000A:C353 0D00000A:1F90 01 00000000:00000000 00:00000000 00000000     0        0 105 1",
            "   5: 0100007F:C354 0100007F:1F90 01 00000000:00000000 00:00000000 00000000     0        0 104 1"]
    (p / "net").mkdir(parents=True)
    (p / "net" / "tcp").write_text(hdr + "\n".join(rows) + "\n")
    (p / "net" / "tcp6").write_text(hdr + "   0: B80D0120000000000000000001000000:01BB B80D0120000000000000000002000000:D431 01 00000000:00000000 00:00000000 00000000     0        0 106 1\n")
    (p / "loadavg").write_text("2.00 1.00 0.50 1/100 999\n")
    (p / "meminfo").write_text("MemTotal:       2000 kB\nMemFree:  10 kB\nMemAvailable:    500 kB\n")
    for pid, comm, cmdline, maps, inodes in [
        (42, "java", "/usr/bin/java\0-Dcatalina.base=/opt/tomcat\0org.apache.catalina.startup.Bootstrap\0",
         "7f0-7f1 r-xp 00000000 08:01 1 /opt/oracle/lib/libclntsh.so.19.1\n7f2-7f3 r-xp 0 08:01 2 /usr/lib/jvm/lib/server/libjvm.so\n", ["101", "102", "103"]),
        (77, "python3", "/usr/bin/python3\0-m\0uvicorn\0app:api\0", "7f0-7f1 r-xp 0 08:01 3 /usr/lib/python3/site-packages/psycopg2/_psycopg.so\n", ["105", "106"]),
    ]:
        (p / str(pid) / "fd").mkdir(parents=True)
        (p / str(pid) / "comm").write_text(comm + "\n")
        (p / str(pid) / "cmdline").write_text(cmdline)
        (p / str(pid) / "maps").write_text(maps)
        (p / str(pid) / "stat").write_text(f"{pid} ({comm}) S 1 1 1 0 -1 0 1 0 0 0 0 0 0 0 20 0 1 0 {pid}000 0 0\n")
        for i, ino in enumerate(inodes):
            os.symlink(f"socket:[{ino}]", p / str(pid) / "fd" / str(i + 3))
    return p


def test_metrics_and_flows_match_agent_py(wo_agent, tmp_path, monkeypatch):
    proc = fake_proc(tmp_path)
    monkeypatch.setattr(pyagent, "PROC", str(proc))
    monkeypatch.setattr(pyagent, "_FP_CACHE", {})
    py_flows = pyagent.tcp_flows()
    srv, R = recorder()
    port = srv.server_address[1]
    p = subprocess.Popen([str(wo_agent), "ship", "--url", f"http://127.0.0.1:{port}/ingest", "--metrics", "--flows", "--env", "prod", "--interval", "60"],
                         env=clean_env(WATCHOVER_PROC=str(proc)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait(lambda: {"metrics.jsonl", "flows.jsonl"} <= {g["name"] for g in R.got})
    finally:
        p.terminate(); p.wait(15); srv.shutdown()
    flows = next(g["body"] for g in R.got if g["name"] == "flows.jsonl")
    host = socket.gethostname()
    lines = flows.decode().splitlines()
    ts = json.loads(lines[0])["ts"]
    want = "".join(json.dumps({"ts": ts, "host": host, "env": "prod", **f}) + "\n" for f in py_flows)   # agent.py's flows_loop body
    assert flows.decode() == want
    assert {json.loads(ln).get("flow.tech") for ln in lines} == {"java,oci,tomcat", "psycopg,python,uvicorn"}
    metrics = json.loads(next(g["body"] for g in R.got if g["name"] == "metrics.jsonl"))
    m_py = pyagent.host_metrics()
    assert list(metrics)[:2] == ["ts", "host"] and list(metrics)[-1] == "env" and metrics["host"] == m_py["host"]
    assert set(metrics) - {"env", "gpu"} == set(m_py) - {"gpu"}
    ncpu = os.cpu_count() or 1
    assert metrics["cpu"] == round(min(100.0, 100.0 * 2.0 / ncpu), 1) and metrics["memory"] == 75.0


def test_discovery_profiles_and_fingerprints_are_agent_py_s():
    """The Go dictionaries are copies: every Linux / macOS profile glob and every fingerprint of agent.py is in them, in order."""
    disc = (ROOT / "goagent/internal/ship/discover.go").read_text()
    flows = (ROOT / "goagent/internal/ship/flows.go").read_text()
    pos = 0
    for name, markers, globs in pyagent.PROFILES:
        if "Windows" in name:
            assert json.dumps(name) not in disc
            continue
        i = disc.index(json.dumps(name), pos)
        for g in markers + globs:
            assert json.dumps(g) in disc[i:], (name, g)
        pos = i
    pos = 0
    for tag, where, needle in pyagent.FINGERPRINTS:
        lit = "{%s, %s, %s}" % (json.dumps(tag), json.dumps(where), json.dumps(needle))
        pos = flows.index(lit, pos) + 1
    assert re.search(r"MaxAutoFiles = %d\b" % pyagent.MAX_AUTO_FILES, disc)


# ---------------------------------------------------------------- installer
@pytest.mark.parametrize("args, why", [
    (["--url", "http://127.0.0.1:1/ingest"], "--token wo_"),
    (["--url", "http://127.0.0.1:1/ingest", "--token", "wo_x", "--interval", "abc"], "--interval"),
    (["--url", "http://127.0.0.1:1/ingest", "--token", "wo_x", "--bogus"], "unknown option"),
    (["--url", "http://127.0.0.1:1/ingest", "--token", "wo_x", "--logs", "/var/log/a'b"], "single quotes"),
])
def test_installer_argument_checks(args, why, tmp_path):
    r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), *args], capture_output=True, text=True, timeout=60,
                       env=clean_env(WO_INSTALL_ROOT=str(tmp_path / "root")))
    assert r.returncode != 0 and why in (r.stdout + r.stderr)
    assert not (tmp_path / "root").exists()


def sandbox(tmp_path: Path):
    root = tmp_path / "root"
    (root / "run/systemd/system").mkdir(parents=True)
    bindir = tmp_path / "stubs"; bindir.mkdir()
    calls = tmp_path / "systemctl.log"
    (bindir / "systemctl").write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\nexit 0\n"); (bindir / "systemctl").chmod(0o755)
    env = clean_env(WO_INSTALL_ROOT=str(root), PATH=f"{bindir}:{os.environ['PATH']}")
    return root, env, calls


@pytest.mark.skipif(not ARCH or platform.system() != "Linux", reason="wo-agent builds for linux amd64 / arm64")
def test_installer_migrates_agent_py_to_wo_agent(wo_agent, tmp_path, monkeypatch):
    from watchover.agents import AgentRegistry
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore, start_receiver
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(wo_agent.parent))                    # the receiver serves the binary + manifest
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    rec, tok = reg.enroll("app-07", "prod", "IST")
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, None, reg)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    root, env, calls = sandbox(tmp_path)
    logs = tmp_path / "logs"; logs.mkdir()
    app = logs / "app.log"; app.write_bytes(b"".join(jl("shipped by agent.py", i) for i in range(20)))
    # an existing agent.py install (as the previous installer left it): unit, env with the token, agent.py, one spooled batch
    adir = root / "opt/watchover-agent"; (adir / "spool").mkdir(parents=True)
    (adir / "agent.py").write_text("# Watchover agent (old)\n")
    (adir / "agent.env").write_text(f"WATCHOVER_URL='{base}/ingest'\nWATCHOVER_TOKEN='{tok}'\nWATCHOVER_LOGS='{logs}/*.log'\nWATCHOVER_METRICS=1\n"
                                    f"WATCHOVER_FLOWS=1\nWATCHOVER_INTERVAL='10'\nWATCHOVER_SPOOL='{adir}/spool'\nAGENT_ENV='prod'\nWATCHOVER_CA=''\n")
    (adir / "spool" / "1760000000000000000__app.log").write_bytes(jl("spooled by agent.py", 0))
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/watchover-agent.service").write_text(f"[Service]\nExecStart=/usr/bin/python3 {adir}/agent.py\n")
    try:
        # the same command as the first install: the fleet key is NOT used again, the host keeps its token and identity
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--url", f"{base}/ingest", "--enroll-key", "wk_not_used_again",
                            "--logs", f"{logs}/*.log", "--env", "prod"], capture_output=True, text=True, timeout=180, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout + r.stderr
        assert "Keeping this host's agent token" in out and "Migrating from agent.py" in out and "▶ Python" not in out
        unit = (root / "etc/systemd/system/watchover-agent.service").read_text()
        binp = root / "usr/local/lib/watchover/wo-agent"
        assert f'ExecStart="{binp}" "ship"' in unit and "agent.py" not in unit and "NoNewPrivileges=yes" in unit
        assert binp.exists() and not (adir / "agent.py").exists()
        envtxt = (adir / "agent.env").read_text()
        assert f"WATCHOVER_TOKEN='{tok}'" in envtxt and f"WATCHOVER_STATE='{adir}/positions.json'" in envtxt and oct((adir / "agent.env").stat().st_mode)[-3:] == "600"
        pos = json.loads((adir / "positions.json").read_text())
        assert [(f["path"], f["off"]) for f in pos["files"]] == [(str(app), app.stat().st_size)]          # taken over at the hand-over
        assert (adir / "spool" / "1760000000000000000__app.log").exists()                                  # agent.py's spool kept
        log = calls.read_text()
        assert "stop watchover-agent" in log and "restart watchover-agent" in log and log.index("stop watchover-agent") < log.index("restart watchover-agent")
        assert (root / "etc/systemd/system/watchover-actuator.service").exists() and (root / "etc/watchover/wo-agent.env").exists()  # the actuator, dormant
        assert "WO_INSECURE_INSTALL=1" in (root / "etc/watchover/wo-agent.env").read_text()                # plain HTTP, no pin: marked
        assert reg.list() and len([a for a in reg.list() if a["name"] == "app-07"]) == 1                   # no second enrolment
        # what systemd would start: the spooled batch is drained, lines after the hand-over arrive once, nothing before it
        append(app, b"".join(jl("after hand-over", i) for i in range(30)))
        n0 = len(store.snapshot())
        p = subprocess.Popen(["bash", "-c", f'set -a; . "{adir}/agent.env"; set +a; exec "{binp}" ship'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            msgs = lambda: [o.message for o in store.snapshot()[n0:]]
            assert wait(lambda: sum(1 for m in msgs() if m.startswith("after hand-over")) >= 30 and "spooled by agent.py 0" in msgs(), 20), msgs()
        finally:
            p.terminate(); p.wait(15)
        got = [o.message for o in store.snapshot()[n0:]]
        assert sorted(m for m in got if m.startswith("after hand-over")) == sorted(f"after hand-over {i}" for i in range(30))
        assert not [m for m in got if m.startswith("shipped by agent.py")]
        assert {o.attributes.get("agent") for o in store.snapshot()[n0:] if o.message.startswith("after")} == {"app-07"}
        # a second run is an upgrade in place: positions kept (not seeded again), token kept
        before = (adir / "positions.json").read_text()
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--url", f"{base}/ingest", "--enroll-key", "wk_not_used_again",
                            "--logs", f"{logs}/*.log", "--no-test"], capture_output=True, text=True, timeout=180, env=env)
        assert r.returncode == 0 and "Migrating" not in r.stdout and (adir / "positions.json").read_text() == before, r.stdout + r.stderr
        # --python-agent goes back to agent.py (the fallback), --uninstall removes everything
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--url", f"{base}/ingest", "--token", tok, "--python-agent", "--no-test",
                            "--logs", f"{logs}/*.log"], capture_output=True, text=True, timeout=180, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "agent.py" in (root / "etc/systemd/system/watchover-agent.service").read_text() and (adir / "agent.py").exists()
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--uninstall"], capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0
        for p in ("opt/watchover-agent", "usr/local/lib/watchover", "etc/watchover", "var/lib/watchover", "usr/lib/watchover",
                  "etc/systemd/system/watchover-agent.service", "etc/systemd/system/watchover-actuator.service"):
            assert not (root / p).exists(), p
    finally:
        srv.shutdown()


@pytest.mark.skipif(not ARCH or platform.system() != "Linux", reason="wo-agent builds for linux amd64 / arm64")
def test_installer_fresh_enrolment_with_wo_agent_and_checksum_pin(wo_agent, tmp_path, monkeypatch):
    from watchover.agents import AgentRegistry
    from watchover.knowledge import Knowledge
    from watchover.live import LiveStore, start_receiver
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(wo_agent.parent))
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    key = reg.enroll_key()
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, None, reg)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    root, env, calls = sandbox(tmp_path)
    try:
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--url", f"{base}/ingest", "--agent-sha256", "0" * 64, "--token", "wo_x"],
                           capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode != 0 and "differs from --agent-sha256" in r.stdout + r.stderr and not (root / "usr/local/lib/watchover/wo-agent").exists()
        import hashlib
        sha = hashlib.sha256(wo_agent.read_bytes()).hexdigest()
        r = subprocess.run(["bash", str(ROOT / "scripts/agent-install.sh"), "--url", f"{base}/ingest", "--enroll-key", key, "--name", "web-09",
                            "--env", "staging", "--agent-sha256", sha, "--no-flows"], capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        adir = root / "opt/watchover-agent"
        envtxt = (adir / "agent.env").read_text()
        assert "WATCHOVER_TOKEN='wo_" in envtxt and "WATCHOVER_FLOWS=0" in envtxt and not (adir / "agent.py").exists()
        assert [a for a in reg.list() if a["name"] == "web-09"] and (adir / "positions.json").exists()
        assert "WO_INSECURE_INSTALL=0" in (root / "etc/watchover/wo-agent.env").read_text()                # pinned: not "güvensiz"
        assert reg.get([a for a in reg.list() if a["name"] == "web-09"][0]["id"])["events"] >= 1          # the self-test hello arrived
    finally:
        srv.shutdown()
