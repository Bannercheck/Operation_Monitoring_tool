"""Agent self-update and the verified install command (src/watchover/agent_update.py, act/keyd.py release signing,
goagent/internal/update, scripts/agent-boot.sh, scripts/agent-install.sh, agent.py's self-migration):
- keyd signs the release it hashes itself, the same files give the same signature, new files the next seq, pins only for
  versions it signed; the dashboard's switch / pause / rollout / pin and the panel's states; the Python receiver's answers;
- end to end with two real wo-agent builds (0.8.0 and 0.9.0): the running 0.8.0 sees the receiver's hint, its update unit
  verifies and installs 0.9.0, the panel shows it, and a pin takes it back to 0.8.0 from the kept binary;
- agent.py moves itself to wo-agent in a sandbox root (a tiny systemd stub runs what the unit files say), and is put back
  when wo-agent does not come up;
- the installer through the panel's own commands: plain http + boot.sh + release key, and https with the edge CA pinned;
- the nginx edge routes every agent path to the receiver.
Go-dependent parts skip without a Go toolchain."""
import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "")
LINUX_GO = pytest.mark.skipif(not ARCH or platform.system() != "Linux", reason="wo-agent builds for linux amd64 / arm64")
sys.path.insert(0, str(ROOT))

from watchover.act.keyd import KeyStore, KeydError, fingerprint, open_export  # noqa: E402
from watchover.agent_update import AgentUpdates, bucket, newer  # noqa: E402
from watchover.agents import AgentRegistry  # noqa: E402
from watchover.knowledge import Knowledge  # noqa: E402


def fake_bin(version: str, extra: bytes = b"") -> bytes:
    return b"\x7fELF fake wo-agent " + extra + b" WO-AGENT-VERSION=" + version.encode() + b"; tail"


class Keys:
    """KeyClient's release calls on an in-process KeyStore (the API reaches keyd the same way, over its socket)."""

    def __init__(self, store: KeyStore):
        self.store = store

    def release(self):
        return self.store.call("release", {})

    def releases(self):
        return self.store.call("releases", {})

    def sign_pin(self, host, version, ttl_s=7 * 86400):
        return self.store.call("sign_pin", {"host": host, "version": version, "ttl_s": ttl_s})


def verify(pub: str, domain: bytes, doc: dict) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    body = base64.b64decode(doc["body"])
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub.split(":", 1)[1])).verify(base64.b64decode(doc["sig"]), domain + body)
    return json.loads(body)


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(fn, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


# ---------------------------------------------------------------- keyd: the release it hashes itself
def test_keyd_signs_the_release_it_hashes_itself(tmp_path):
    rd = tmp_path / "rel"; rd.mkdir()
    (rd / "wo-agent-linux-amd64").write_bytes(fake_bin("1.2.3", b"amd64"))
    (rd / "wo-agent-linux-arm64").write_bytes(fake_bin("1.2.3", b"arm64"))
    ks = KeyStore(tmp_path / "keys", durable=False, release_dir=str(rd))
    r = ks.release()
    assert not ks.exists()                                            # independent of the act keys and of act_mode
    body = verify(r["key"], b"wo-release-v1\n", r)
    assert body["version"] == "1.2.3" and body["seq"] == 1 and r["fp"] == fingerprint(r["key"]) and len(r["fp"]) == 50
    assert body["files"]["wo-agent-linux-arm64"]["sha256"] == sha(rd / "wo-agent-linux-arm64")
    assert body["files"]["agent-install.sh"]["sha256"] == sha(ROOT / "scripts/agent-install.sh")
    assert body["files"]["agent-boot.sh"]["sha256"] == sha(ROOT / "scripts/agent-boot.sh") and "agent.py" in body["files"]
    assert ks.release() == r                                          # same files: same body, same (deterministic) signature
    (rd / "wo-agent-linux-arm64").write_bytes(fake_bin("1.2.3", b"arm64 rebuilt"))
    r2 = ks.release()
    assert verify(r["key"], b"wo-release-v1\n", r2)["seq"] == 2 and r2["key"] == r["key"]
    with pytest.raises(KeydError):                                    # a pin only for a version keyd signed
        ks.sign_pin("app-07", "9.9.9")
    pin = ks.sign_pin("app-07", "1.2.3", 3600)
    pb = verify(r["key"], b"wo-pin-v1\n", pin)
    assert pb["host"] == "app-07" and pb["version"] == "1.2.3" and set(pb["files"]) == {"wo-agent-linux-amd64", "wo-agent-linux-arm64"}
    with pytest.raises(Exception):                                    # the pin domain never verifies as a release
        verify(r["key"], b"wo-release-v1\n", pin)
    (rd / "wo-agent-linux-arm64").write_bytes(fake_bin("1.2.4"))
    with pytest.raises(KeydError, match="disagree"):
        ks.release()
    (rd / "wo-agent-linux-arm64").write_bytes(b"\x7fELF no marker")
    with pytest.raises(KeydError, match="marker"):
        ks.release()
    assert ks.status()["release_fingerprint"] == r["fp"]
    ks.create()                                                       # the act keys; the backup carries release.key too
    tok = ks.export_begin("first-half-1", "a")["token"]
    files = open_export(ks.export_finish(tok, "second-half-2", "b")["blob"], "first-half-1", "second-half-2")
    assert "release.key" in files and "releases.json" in files


# ---------------------------------------------------------------- the dashboard: policy, states, export, install command
def dash(tmp_path, monkeypatch, versions=("0.9.0",)):
    rd = tmp_path / "rel"; rd.mkdir(exist_ok=True)
    ks = KeyStore(tmp_path / "keys", durable=False, release_dir=str(rd))
    for v in versions:
        (rd / "wo-agent-linux-amd64").write_bytes(fake_bin(v, b"amd64"))
        (rd / "wo-agent-linux-arm64").write_bytes(fake_bin(v, b"arm64"))
        ks.release()
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(rd))
    for k in ("WATCHOVER_EDGE", "WATCHOVER_EDGE_HOST", "WATCHOVER_EDGE_HTTPS_PORT", "WATCHOVER_INGEST_URL", "WATCHOVER_AGENT_CA"):
        monkeypatch.delenv(k, raising=False)
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    upd = AgentUpdates(reg, lambda: Keys(ks), home=str(tmp_path / "home"))
    return reg, upd, ks, rd


def test_dashboard_policy_states_export_and_install_command(tmp_path, monkeypatch):
    reg, upd, ks, rd = dash(tmp_path, monkeypatch, ("0.8.0", "0.9.0"))
    rec, tok = reg.enroll("app-07", "prod")
    aid = rec["id"]
    st = upd.status()
    assert st["signed"] and st["version"] == "0.9.0" and st["enabled"] and st["pct"] == 100 and st["fp"].startswith("SHA256:")
    assert [v["version"] for v in upd.versions()] == ["0.9.0", "0.8.0"]
    a = reg.get(aid)
    assert (upd.decide(a, "wo-agent", "0.8.0")["update"], upd.decide(a, "wo-agent", "0.8.0")["reason"]) == (True, "newer")
    assert upd.decide(a, "wo-agent", "0.9.0")["reason"] == "current" and not upd.decide(a, "wo-agent", "1.0.0")["update"]
    assert upd.decide(a, "agent.py", "0.6.0")["reason"] == "migrate"
    upd.set_paused(aid, True)
    assert upd.decide(a, "wo-agent", "0.8.0")["reason"] == "paused"
    reg.touch(aid, "10.0.0.7", 1, version="0.8.0", engine="wo-agent", update="")
    assert upd.state(reg.get(aid))["state"] == "paused"
    upd.set_paused(aid, False)
    upd.set_settings(enabled=False)
    assert upd.decide(a, "wo-agent", "0.8.0")["reason"] == "disabled" and upd.state(reg.get(aid))["state"] == "off"
    upd.set_settings(enabled=True, pct=0)
    assert upd.decide(a, "wo-agent", "0.8.0")["reason"] == "rollout" and upd.state(reg.get(aid))["state"] == "waiting"
    upd.set_settings(pct=100)
    assert upd.state(reg.get(aid))["state"] == "updating"
    reg.touch(aid, "10.0.0.7", 1, version="0.8.0", engine="wo-agent", update="rolled_back;0.9.0;0.8.0;2026-10-16T10:00:00Z;no hello in 120s")
    s = upd.state(reg.get(aid))
    assert s["state"] == "rolled_back" and "no hello" in s["error"]
    reg.touch(aid, "10.0.0.7", 1, version="0.9.0", engine="wo-agent", update="updated;0.9.0;0.8.0;2026-10-16T10:01:00Z;")
    assert upd.state(reg.get(aid))["state"] == "current"
    reg.touch(aid, "10.0.0.7", 1, version="0.6.0", engine="agent.py", update="unsupported;;agent.py 0.6.0;x;not root")
    assert upd.state(reg.get(aid))["state"] == "unsupported"
    # the rollout bucket is the Go receiver's (receiver/agentupdate_test.go has the same vectors)
    assert (bucket(7, "0.9.0"), bucket(1, "0.8.0"), bucket(42, "1.2.3")) == (2, 82, 76)
    assert newer("0.10.0", "0.9.9") and not newer("1.0.0-rc1", "1.0.0") and newer("1.0.0", "1.0.0-rc1")
    # "bu sürüme döndür": a pin keyd signs for this host, only for a version it signed before
    with pytest.raises(KeydError):
        upd.set_pin(aid, "0.7.0")
    reg.touch(aid, "10.0.0.7", 1, version="0.9.0", engine="wo-agent", update="updated;0.9.0;0.8.0;2026-10-16T10:01:00Z;")
    upd.set_pin(aid, "0.8.0")
    d = upd.decide(a, "wo-agent", "0.9.0")
    assert d["update"] and d["version"] == "0.8.0" and verify(ks.release()["key"], b"wo-pin-v1\n", d["pin"])["host"] == "app-07"
    assert upd.state(reg.get(aid))["state"] == "updating" and upd.state(reg.get(aid))["pin"] == "0.8.0"
    # the Go receiver's view: agent-update.json and the signed files under <spool>/release
    spool = tmp_path / "spool"; spool.mkdir()
    assert upd.export(spool) and not upd.export(spool)                # written once, then only on change
    doc = json.loads((spool / "agent-update.json").read_text())
    assert doc["release"]["version"] == "0.9.0" and doc["enabled"] and doc["pct"] == 100 and str(aid) in doc["pins"]
    assert sha(spool / "release" / "wo-agent-linux-amd64") == sha(rd / "wo-agent-linux-amd64")
    assert sha(spool / "release" / "agent-boot.sh") == sha(ROOT / "scripts/agent-boot.sh")
    upd.set_pin(aid, "")
    assert upd.export(spool) and json.loads((spool / "agent-update.json").read_text())["pins"] == {}
    # the install command: boot.sh by its sha256, the release key's fingerprint; https with the edge CA pinned
    info = upd.install_info()
    fp = st["fp"]
    assert info["signed"] and info["boot_sha256"] == sha(ROOT / "scripts/agent-boot.sh") and not info["https"]
    assert info["http"].startswith('f=$(mktemp) && curl -fsSL -o "$f" {BASE}/agent/boot.sh && echo "' + info["boot_sha256"] + '  $f" | sha256sum -c --quiet')
    assert f"--url {{BASE}}/ingest --release-key {fp} {{AUTH}}" in info["http"]
    ca = tmp_path / "edge-root.crt"; ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("WATCHOVER_EDGE", "1"); monkeypatch.setenv("WATCHOVER_EDGE_HOST", "wo.firma.local")
    monkeypatch.setenv("WATCHOVER_EDGE_HTTPS_PORT", "8443"); monkeypatch.setenv("WATCHOVER_AGENT_CA", str(ca))
    info = upd.install_info()
    assert info["https"].startswith('f=$(mktemp) && curl -fsSLk -o "$f" https://wo.firma.local:8443/agent/boot.sh')
    assert f"--url https://wo.firma.local:8443/ingest --ca-sha256 {sha(ca)} --release-key {fp} {{AUTH}}" in info["https"]
    assert (spool / "agent-ca.pem").write_text(ca.read_text()) and upd.install_info(str(spool))["ca_sha256"] == sha(ca)
    # without keyd: the old command, no self-update
    bare = AgentUpdates(reg, lambda: None, home=str(tmp_path / "home2"))
    assert not bare.install_info()["signed"] and bare.install_info()["http"] == bare.install_info()["legacy"]
    assert bare.decide(a, "wo-agent", "0.8.0") == {"release": None, "update": False, "reason": "unsigned"}


def test_python_receiver_update_endpoint_manifest_and_hint(tmp_path, monkeypatch):
    from watchover.live import LiveStore, start_receiver
    reg, upd, ks, rd = dash(tmp_path, monkeypatch)
    rec, tok = reg.enroll("app-07", "prod")
    srv = start_receiver(LiveStore(spool=tmp_path / "live.jsonl"), 0, None, reg, updates=upd)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/agent/update", timeout=10)
        assert e.value.code == 401
        req = urllib.request.Request(base + "/agent/update", headers={"Authorization": f"Bearer {tok}", "X-Agent-Version": "0.8.0", "X-Agent-Engine": "wo-agent"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert d["update"] and d["version"] == "0.9.0" and d["agent"] == "app-07" and d["release"]["sig"]
        m = json.loads(urllib.request.urlopen(base + "/agent/manifest.json", timeout=10).read())
        assert m["release_version"] == "0.9.0" and m["release_fp"] == upd.status()["fp"] and "agent-boot.sh" in m["files"]
        assert urllib.request.urlopen(base + "/agent/boot.sh", timeout=10).read() == (ROOT / "scripts/agent-boot.sh").read_bytes()
        req = urllib.request.Request(base + "/ingest", data=b'{"msg":"x"}\n', method="POST", headers={
            "Authorization": f"Bearer {tok}", "X-Agent-Version": "0.8.0", "X-Agent-Engine": "wo-agent", "X-Agent-Update": "failed;0.9.0;0.8.0;t;sha"})
        r = urllib.request.urlopen(req, timeout=10)
        assert r.headers.get("X-Watchover-Update") == "0.9.0"
        row = reg.get(rec["id"])
        assert (row["agent_version"], row["agent_engine"], row["agent_update"]) == ("0.8.0", "wo-agent", "failed;0.9.0;0.8.0;t;sha")
        assert upd.state(row)["state"] == "failed"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- real builds
@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    """wo-agent 0.8.0 (this source) and 0.9.0 (the same source, version bumped), each in a directory of its own."""
    if GO is None:
        pytest.skip("no Go toolchain")
    if not ARCH or platform.system() != "Linux":
        pytest.skip("wo-agent builds for linux amd64 / arm64")
    out = {}
    src = tmp_path_factory.mktemp("src") / "goagent"
    shutil.copytree(ROOT / "goagent", src, ignore=shutil.ignore_patterns("dist"))
    ship = src / "internal/ship/ship.go"
    cur = ship.read_text()
    assert 'const Version = "0.8.0"' in cur, "bump these tests together with ship.Version"
    for v in ("0.8.0", "0.9.0"):
        ship.write_text(cur.replace('const Version = "0.8.0"', f'const Version = "{v}"'))
        d = tmp_path_factory.mktemp("dist-" + v)
        subprocess.run([GO, "build", "-trimpath", "-o", str(d / f"wo-agent-linux-{ARCH}"), "."], cwd=src, check=True, capture_output=True,
                       timeout=300, env=dict(os.environ, CGO_ENABLED="0"))
        out[v] = d
    return out


SYSTEMCTL = r"""#!/bin/bash
# a tiny systemd for the tests: restart / stop / start / try-restart run what the sandbox's unit file says, in the background
echo "$*" >> "$STUB_LOG"
U="$WO_INSTALL_ROOT/etc/systemd/system"
run_unit() {
  local f="$U/$1.service" line envf
  [ -f "$f" ] || return 0
  case ",${STUB_NOSTART:-}," in *",$1,"*) return 0;; esac
  line="$(sed -n 's/^ExecStart=//p' "$f" | head -1)"; envf="$(sed -n 's/^EnvironmentFile=//p' "$f" | head -1)"
  ( if [ -n "$envf" ]; then set -a; . "$envf"; set +a; fi; eval "exec $line" ) >>"$STUB_RUN/$1.out" 2>&1 &
  echo $! > "$STUB_RUN/$1.pid"
}
stop_unit() {
  local p="$STUB_RUN/$1.pid" i
  [ -f "$p" ] || return 0
  kill "$(cat "$p")" 2>/dev/null
  for i in $(seq 100); do kill -0 "$(cat "$p")" 2>/dev/null || break; sleep 0.1; done
  rm -f "$p"
}
words=(); for a in "$@"; do case "$a" in -*) ;; *) words+=("$a");; esac; done
verb="${words[0]:-}"
for u in "${words[@]:1}"; do
  u="${u%.service}"
  case "$verb" in
    restart) stop_unit "$u"; run_unit "$u";;
    stop) stop_unit "$u";;
    start) run_unit "$u";;
    try-restart) if [ -f "$STUB_RUN/$u.pid" ]; then stop_unit "$u"; run_unit "$u"; fi;;
  esac
done
exit 0
"""
SYSTEMD_RUN = r"""#!/bin/bash
echo "systemd-run $*" >> "$STUB_LOG"
while [ $# -gt 0 ]; do case "$1" in --unit|-p) shift 2;; --*) shift;; *) break;; esac; done
nohup "$@" >>"$STUB_RUN/migrate.out" 2>&1 &
exit 0
"""


class Sandbox:
    """A sandbox root (WO_INSTALL_ROOT) with stubs for systemctl, systemd-run and sudo (which keeps the environment)."""

    def __init__(self, tmp: Path):
        self.root = tmp / "root"
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.stubs = tmp / "stubs"; self.stubs.mkdir()
        self.run = tmp / "run"; self.run.mkdir()
        self.log = tmp / "systemctl.log"; self.log.write_text("")
        for name, body in (("systemctl", SYSTEMCTL), ("systemd-run", SYSTEMD_RUN), ("sudo", '#!/bin/sh\nexec "$@"\n')):
            (self.stubs / name).write_text(body); (self.stubs / name).chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("WATCHOVER_", "AGENT_ENV", "WO_"))}
        self.env.update(WO_INSTALL_ROOT=str(self.root), PATH=f"{self.stubs}:{os.environ['PATH']}", STUB_LOG=str(self.log), STUB_RUN=str(self.run))
        self.unitdir = self.root / "etc/systemd/system"
        self.bin = self.root / "usr/local/lib/watchover/wo-agent"
        self.adir = self.root / "opt/watchover-agent"
        self.upd = self.root / "var/lib/watchover/update"

    def systemctl(self, *args):
        subprocess.run([str(self.stubs / "systemctl"), *args], env=self.env, check=True, timeout=60)

    def stop_all(self):
        for p in self.run.glob("*.pid"):
            try:
                os.kill(int(p.read_text()), 15)
            except (OSError, ValueError):
                pass

    def version(self, path=None) -> str:
        return subprocess.run([str(path or self.bin), "version"], capture_output=True, text=True, timeout=30).stdout.split()[1]


@LINUX_GO
def test_wo_agent_updates_itself_from_a_signed_newer_build_and_goes_back_on_a_pin(builds, tmp_path, monkeypatch):
    from watchover.live import LiveStore, start_receiver
    ks = KeyStore(tmp_path / "keys", durable=False, release_dir=str(builds["0.8.0"]))
    ks.release()                                                       # 0.8.0 was signed first (the release a pin can go back to)
    ks.release_dir = str(builds["0.9.0"])
    rel = ks.release()
    assert rel["version"] == "0.9.0"
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(builds["0.9.0"]))   # the receiver serves the newer build
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    rec, tok = reg.enroll("app-07", "prod")
    upd = AgentUpdates(reg, lambda: Keys(ks), home=str(tmp_path / "home"))
    srv = start_receiver(LiveStore(spool=tmp_path / "live.jsonl"), 0, None, reg, updates=upd)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    sb = Sandbox(tmp_path)
    sb.bin.parent.mkdir(parents=True); shutil.copy(builds["0.8.0"] / f"wo-agent-linux-{ARCH}", sb.bin); sb.bin.chmod(0o755)
    sb.upd.mkdir(parents=True); (sb.adir / "spool").mkdir(parents=True)
    (sb.adir / "agent.env").write_text(
        f"WATCHOVER_URL='{base}/ingest'\nWATCHOVER_TOKEN='{tok}'\nWATCHOVER_METRICS=1\nWATCHOVER_INTERVAL='1'\nWATCHOVER_SPOOL='{sb.adir}/spool'\n"
        f"WATCHOVER_STATE='{sb.adir}/positions.json'\nWATCHOVER_RELEASE_KEY='{rel['key']}'\nWATCHOVER_AGENT_NAME='app-07'\nWO_UPDATE_DIR='{sb.upd}'\n")
    (sb.unitdir / "watchover-agent.service").write_text(f'[Service]\nEnvironmentFile={sb.adir}/agent.env\nExecStart="{sb.bin}" "ship"\n')
    (sb.unitdir / "watchover-update.service").write_text(f'[Service]\nType=oneshot\nEnvironmentFile={sb.adir}/agent.env\nExecStart="{sb.bin}" update --gate 60s\n')
    row = lambda: reg.get(rec["id"])  # noqa: E731
    try:
        sb.systemctl("restart", "watchover-agent")                      # 0.8.0 runs; its first answer carries X-Watchover-Update: 0.9.0
        assert wait(lambda: (row()["agent_update"] or "").startswith("updated;0.9.0;0.8.0") and row()["agent_version"] == "0.9.0", 90), \
            (row(), sb.log.read_text(), (sb.run / "watchover-update.out").read_text() if (sb.run / "watchover-update.out").exists() else "")
        assert sb.version() == "0.9.0" and sb.version(str(sb.bin) + ".prev") == "0.8.0"
        assert json.loads((sb.upd / "state.json").read_text())["phase"] == "updated"
        log = sb.log.read_text()
        assert "start --no-block watchover-update.service" in log and "try-restart watchover-actuator" in log
        assert upd.state(row())["state"] == "current"
        # "bu sürüme döndür" 0.8.0: the kept binary comes back (nothing older is ever downloaded or accepted without the pin)
        upd.set_pin(rec["id"], "0.8.0")
        assert wait(lambda: row()["agent_version"] == "0.8.0" and (row()["agent_update"] or "").startswith("updated;0.8.0;0.9.0"), 90), \
            (row(), (sb.run / "watchover-update.out").read_text())
        assert sb.version() == "0.8.0" and upd.state(row())["state"] == "current" and upd.state(row())["pin"] == "0.8.0"
    finally:
        sb.stop_all()
        srv.shutdown()


def write_agent_py_install(sb: Sandbox, base: str, tok: str, logs: Path, key: str = ""):
    (sb.adir / "spool").mkdir(parents=True)
    shutil.copy(ROOT / "agent.py", sb.adir / "agent.py")
    env = (f"WATCHOVER_URL='{base}/ingest'\nWATCHOVER_TOKEN='{tok}'\nWATCHOVER_LOGS='{logs}/*.log'\nWATCHOVER_METRICS=1\nWATCHOVER_FLOWS=0\n"
           f"WATCHOVER_INTERVAL='10'\nWATCHOVER_SPOOL='{sb.adir}/spool'\nAGENT_ENV='prod'\nWATCHOVER_CA=''\nWATCHOVER_RELEASE_KEY='{key}'\n")
    (sb.adir / "agent.env").write_text(env)
    (sb.unitdir / "watchover-agent.service").write_text(f'[Service]\nEnvironmentFile={sb.adir}/agent.env\nExecStart="/usr/bin/python3" "{sb.adir}/agent.py"\n')
    return {k: v.strip("'") for k, v in (ln.split("=", 1) for ln in env.splitlines())}


@LINUX_GO
def test_agent_py_migrates_itself_to_wo_agent_and_is_put_back_when_it_fails(builds, tmp_path, monkeypatch):
    import agent as pyagent
    from watchover.live import LiveStore, start_receiver
    ks = KeyStore(tmp_path / "keys", durable=False, release_dir=str(builds["0.8.0"]))
    rel = ks.release()
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(builds["0.8.0"]))
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    rec, tok = reg.enroll("app-07", "prod")
    upd = AgentUpdates(reg, lambda: Keys(ks), home=str(tmp_path / "home"))
    srv = start_receiver(LiveStore(spool=tmp_path / "live.jsonl"), 0, None, reg, updates=upd)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    logs = tmp_path / "logs"; logs.mkdir()
    app = logs / "app.log"; app.write_text("".join(json.dumps({"msg": f"before {i}"}) + "\n" for i in range(5)))

    def migrate(sb: Sandbox, envd: dict) -> Path:
        for k, v in {**sb.env, **envd}.items():
            monkeypatch.setenv(k, v)
        m = pyagent.Migration()
        assert m.blocker() == ""
        m.sender = pyagent.Sender(envd["WATCHOVER_URL"], tok, "app-07")
        assert m.check() == 600                                         # verified, the installer runs under (stub) systemd-run
        assert json.loads((sb.upd / "state.json").read_text())["phase"] in ("migrating", "updated")
        assert wait(lambda: (sb.upd / "migrate.rc").exists(), 180), (sb.run / "migrate.out").read_text()
        return m

    try:
        # 1) wo-agent does not come up within the gate: the installer puts agent.py's unit back, agent.py reports "failed"
        sb = Sandbox(tmp_path / "a")
        envd = write_agent_py_install(sb, base, tok, logs, rel["key"])
        sb.env["STUB_NOSTART"] = "watchover-agent"
        m = migrate(sb, {**envd, "WO_MIGRATE_GATE": "3", "STUB_NOSTART": "watchover-agent"})
        out = (sb.run / "migrate.out").read_text()
        assert (sb.upd / "migrate.rc").read_text().strip() != "0" and "agent.py runs again" in out, out
        assert "agent.py" in (sb.unitdir / "watchover-agent.service").read_text() and (sb.adir / "agent.py").exists()
        m.check()
        st = json.loads((sb.upd / "state.json").read_text())
        assert st["phase"] == "failed" and "installer exit" in st["error"] and m.header().startswith("failed;0.8.0;agent.py")
        sb.stop_all()
        # 2) the real thing: token, identity, spool and positions kept, wo-agent reaches the receiver, agent.py is gone
        monkeypatch.delenv("STUB_NOSTART", raising=False)
        sb = Sandbox(tmp_path / "b")
        envd = write_agent_py_install(sb, base, tok, logs, rel["key"])
        (sb.adir / "spool" / "1760000000000000000__app.log").write_text(json.dumps({"msg": "spooled by agent.py"}) + "\n")
        migrate(sb, {**envd, "WO_MIGRATE_GATE": "60"})
        out = (sb.run / "migrate.out").read_text()
        assert (sb.upd / "migrate.rc").read_text().strip() == "0", out
        assert "Migrating from agent.py" in out and "reached the receiver" in out and "Signed release 0.8.0 verified" in out
        assert tok not in sb.log.read_text() and "--token-file" in sb.log.read_text()     # the token never on a command line (security review)
        assert not list(sb.upd.glob("token-*"))                                        # the installer read the file once and removed it
        unit = (sb.unitdir / "watchover-agent.service").read_text()
        assert f'ExecStart="{sb.bin}" "ship"' in unit and not (sb.adir / "agent.py").exists() and not (sb.adir / ".migrate").exists()
        env = (sb.adir / "agent.env").read_text()
        assert f"WATCHOVER_TOKEN='{tok}'" in env and f"WATCHOVER_RELEASE_KEY='{rel['key']}'" in env and f"WO_UPDATE_DIR='{sb.upd}'" in env
        assert "WATCHOVER_AGENT_NAME='app-07'" in env
        assert json.loads((sb.adir / "positions.json").read_text())["files"][0]["off"] == app.stat().st_size
        st = json.loads((sb.upd / "state.json").read_text())
        assert (st["phase"], st["from"], st["to"]) == ("updated", "agent.py", "0.8.0")
        assert (sb.unitdir / "watchover-update.service").exists() and (sb.unitdir / "watchover-update.timer").exists()
        assert "WO_INSECURE_INSTALL=0" in (sb.root / "etc/watchover/wo-agent.env").read_text()   # verified with the pinned key
        assert wait(lambda: reg.get(rec["id"])["agent_version"] == "0.8.0" and reg.get(rec["id"])["agent_engine"] == "wo-agent", 30)
        assert wait(lambda: (reg.get(rec["id"])["agent_update"] or "").startswith("updated;0.8.0;agent.py"), 30)
        assert len([a for a in reg.list() if a["name"] == "app-07"]) == 1
        sb.stop_all()
        # 3) what cannot migrate says why (no pinned key: installed before signed releases)
        sb = Sandbox(tmp_path / "c")
        envd = write_agent_py_install(sb, base, tok, logs, "")
        for k, v in {**sb.env, **envd}.items():
            monkeypatch.setenv(k, v)
        m = pyagent.Migration()
        assert "no release key pinned" in m.blocker()
        m.run(pyagent.Sender(envd["WATCHOVER_URL"], tok, "app-07"), threading.Event())
        assert m.header().startswith("unsupported;;agent.py")
    finally:
        sb.stop_all()
        srv.shutdown()


# ---------------------------------------------------------------- the installer through the panel's commands
def make_tls(tmp: Path):
    """A CA and a server certificate for localhost / 127.0.0.1 (the edge with its own CA)."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Watchover test edge CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tmp / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp / "ca.pem", tmp / "leaf.pem", tmp / "leaf.key"


@LINUX_GO
def test_installer_http_checksum_path_and_https_with_the_edge_ca_pinned(builds, tmp_path, monkeypatch):
    from watchover.live import LiveStore, make_handler, start_receiver
    ks = KeyStore(tmp_path / "keys", durable=False, release_dir=str(builds["0.8.0"]))
    rel = ks.release()
    monkeypatch.setenv("WATCHOVER_AGENT_BIN_DIR", str(builds["0.8.0"]))
    reg = AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    key = reg.enroll_key()
    upd = AgentUpdates(reg, lambda: Keys(ks), home=str(tmp_path / "home"))
    store = LiveStore(spool=tmp_path / "live.jsonl")
    srv = start_receiver(store, 0, None, reg, updates=upd)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    ca, leaf, leaf_key = make_tls(tmp_path)
    monkeypatch.setenv("WATCHOVER_AGENT_CA", str(ca))                    # served as /agent/ca.crt (the Go receiver: WO_AGENT_CA)
    hsrv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, None, reg, None, None, upd))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(str(leaf), str(leaf_key))
    hsrv.socket = ctx.wrap_socket(hsrv.socket, server_side=True)
    hsrv.daemon_threads = True
    threading.Thread(target=hsrv.serve_forever, daemon=True).start()
    hport = hsrv.server_address[1]
    monkeypatch.setenv("WATCHOVER_EDGE", "1"); monkeypatch.setenv("WATCHOVER_EDGE_HOST", "localhost")
    monkeypatch.setenv("WATCHOVER_EDGE_HTTPS_PORT", str(hport))
    info = upd.install_info()

    def run(cmd: str, sb: Sandbox, auth: str):
        c = cmd.replace("{BASE}", base).replace("{AUTH}", auth)
        return subprocess.run(["bash", "-c", c], capture_output=True, text=True, timeout=240, env=sb.env)
    try:
        # plain http: boot.sh by its sha256, the signed manifest by the release key; recorded secure, key pinned, self-update set up
        sb = Sandbox(tmp_path / "http")
        r = run(info["http"], sb, f"--enroll-key {key} --name web-01 --env prod --no-flows")
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "Installer verified by boot.sh" in out and "Signed release 0.8.0 verified" in out
        env = (sb.adir / "agent.env").read_text()
        assert f"WATCHOVER_RELEASE_KEY='{rel['key']}'" in env and "WATCHOVER_AGENT_NAME='web-01'" in env
        assert "WO_INSECURE_INSTALL=0" in (sb.root / "etc/watchover/wo-agent.env").read_text()
        assert 'ExecStart="' + str(sb.bin) + '" update' in (sb.unitdir / "watchover-update.service").read_text()
        assert "enable --now watchover-update.timer" in sb.log.read_text()
        sb.stop_all()
        # a command with another release key, or a changed boot.sh, installs nothing
        sb = Sandbox(tmp_path / "wrongkey")
        bad = info["http"].replace(rel["fp"], "SHA256:" + "A" * 43)
        r = run(bad, sb, f"--enroll-key {key} --name web-02")
        assert r.returncode != 0 and "is not the one in the command" in r.stdout + r.stderr and not sb.bin.exists()
        r = run(info["http"].replace(info["boot_sha256"], "0" * 64), sb, f"--enroll-key {key} --name web-02")
        assert r.returncode != 0 and not sb.bin.exists() and not (sb.adir / "agent.env").exists()
        # without the release key over plain http (the old command): installed, but "güvensiz kurulum", key pinned on first use
        sb = Sandbox(tmp_path / "legacy")
        r = run(info["legacy"], sb, f"--enroll-key {key} --name web-03")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "WO_INSECURE_INSTALL=1" in (sb.root / "etc/watchover/wo-agent.env").read_text()
        sb.stop_all()
        # https with the edge's own CA: -k only for boot.sh (checked by sha256), the CA by --ca-sha256, then real TLS everywhere
        assert info["https"].startswith('f=$(mktemp) && curl -fsSLk -o "$f" https://localhost:') and f"--ca-sha256 {sha(ca)}" in info["https"]
        sb = Sandbox(tmp_path / "https")
        r = run(info["https"], sb, f"--enroll-key {key} --name web-04 --env prod")
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        env = (sb.adir / "agent.env").read_text()
        cadst = sb.root / "etc/watchover/ca.pem"
        assert f"WATCHOVER_URL='https://localhost:{hport}/ingest'" in env and f"WATCHOVER_CA='{cadst}'" in env
        assert cadst.read_bytes() == ca.read_bytes() and oct(cadst.stat().st_mode)[-3:] == "644"
        assert "WO_INSECURE_INSTALL=0" in (sb.root / "etc/watchover/wo-agent.env").read_text()
        assert f"WO_CA='{cadst}'" in (sb.root / "etc/watchover/wo-agent.env").read_text()
        assert [a for a in reg.list() if a["name"] == "web-04"][0]["events"] >= 1      # the self-test hello arrived over TLS
        sb.stop_all()
        sb = Sandbox(tmp_path / "https-badca")
        r = run(info["https"].replace(sha(ca), "f" * 64), sb, f"--enroll-key {key} --name web-05")
        assert r.returncode != 0 and "does not match --ca-sha256" in r.stdout + r.stderr and not (sb.adir / "agent.env").exists()
    finally:
        sb.stop_all()
        srv.shutdown(); hsrv.shutdown()


# ---------------------------------------------------------------- the edge and the images
def test_edge_routes_every_agent_path_to_the_receiver():
    txt = (ROOT / "docker/nginx/watchover.conf.template").read_text()
    recv = [ln.split("{")[0].strip() for ln in txt.splitlines() if "proxy_pass http://$wo_receiver" in ln]
    for loc in ("location ~ ^/(ingest|enroll)", "location = /agent.py", "location ^~ /agent/", "location = /rum", "location = /rum.js",
                "location = /mobile", "location ^~ /v1/"):
        assert loc in recv, loc                                                           # prefix / exact / regex: all win over "location /"
    assert "set $wo_receiver receiver:8600;" in txt
    edge = (ROOT / "docker-compose.edge.yml").read_text()
    assert "receiver:" in edge and "WO_AGENT_CA: /certs/watchover-ca.crt" in edge and "WO_AGENT_CA_INTERNAL: /certs/watchover-root.crt" in edge
    assert "./certs:/certs:ro" in edge
    sh = (ROOT / "watchover.sh").read_text()
    assert "edge_cert()" in sh and "scripts/edge-cert.sh certs" in sh
    # keyd hashes <repo>/goagent/dist in the dashboard image: the image must carry the binaries there
    df = (ROOT / "Dockerfile").read_text()
    assert "COPY --from=goagent /out/ ./goagent/dist/" in df and "GOARCH=arm64" in df
    assert "COPY scripts/agent-boot.sh /app/agent/agent-boot.sh" in (ROOT / "Dockerfile.receiver").read_text()
    for f in ("scripts/agent-boot.sh", "scripts/agent-install.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / f)]).returncode == 0
