"""watchover.sh drives Docker Compose; here Docker is a fake on PATH that records the calls, so the script's own logic
(.env generation, secrets, image tag, command order, banner) is checked without a daemon."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose version") echo "Docker Compose version v2.29" ;;
  "compose ps") [[ "$*" == *--format* ]] && { echo "dashboard healthy"; echo "ollama healthy"; } || echo "NAME  STATUS"; ;;
  "compose config") echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
  "info --format") case "$3" in *NCPU*) echo 10 ;; *MemTotal*) echo 17179869184 ;; esac ;;
esac
exit 0
'''


@pytest.fixture
def stage(tmp_path):
    for f in ("watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", ".env.docker.example", "pyproject.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / "bin").mkdir(); fake = tmp_path / "bin" / "docker"; fake.write_text(FAKE); fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(tmp_path / "calls.log"), WATCHOVER_VERIFY_TRIES="1")
    return tmp_path, env


def run(stage, *args):
    d, env = stage
    return subprocess.run(["bash", str(d / "watchover.sh"), *args], cwd=d, env=env, capture_output=True, text=True, timeout=60)


def test_install_writes_env_builds_and_starts(stage):
    d, _ = stage
    r = run(stage, "install")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    pw = [l for l in env.splitlines() if l.startswith("POSTGRES_PASSWORD=")][0].split("=", 1)[1]
    key = [l for l in env.splitlines() if l.startswith("MCP_API_KEY=")][0].split("=", 1)[1]
    assert len(pw) == 48 and pw != "change-me" and len(key) == 48 and "#MCP_API_KEY" not in env
    version = [l for l in (ROOT / "pyproject.toml").read_text().splitlines() if l.startswith("version")][0].split('"')[1]
    assert f"WATCHOVER_TAG={version}" in env and "WATCHOVER_IMAGE=watchover" in env
    assert oct((d / ".env").stat().st_mode)[-3:] == "600" and (d / "datasets").is_dir() and (d / "backups").is_dir()
    # sizing follows the machine Docker reports (10 CPUs, 16 GB here) in shares: the dashboard most CPUs, the LLM and the trainer a part
    for k, v in {"WATCHOVER_SIZING": "auto", "WATCHOVER_CPUS": "8", "WATCHOVER_MEMORY": "4g", "WATCHOVER_RECEIVER_CPUS": "2", "WATCHOVER_PARSE_WORKERS": "3",
                 "WATCHOVER_PG_CPUS": "2", "WATCHOVER_PG_MEMORY": "2g", "WATCHOVER_PG_SHARED_BUFFERS": "512MB", "WATCHOVER_PG_CACHE": "8GB",
                 "WATCHOVER_LLM_CPUS": "3", "WATCHOVER_LLM_THREADS": "3", "WATCHOVER_LLM_MEMORY": "4g", "WATCHOVER_TRAIN_CPUS": "5", "WATCHOVER_TRAIN_MEMORY": "4g",
                 "WATCHOVER_LLM_KEEPALIVE": "30m", "WATCHOVER_SPOOL_MAX_MB": "2048"}.items():
        assert f"{k}={v}\n" in env, (k, v)
    calls = (d / "calls.log").read_text()
    assert "compose build" in calls and f"--build-arg VERSION={version}" in calls and "compose up -d --remove-orphans" in calls
    assert "compose build receiver" in calls                                   # the Go receiver image is built with the dashboard
    assert calls.index("compose build") < calls.index("compose up")
    assert "8501" in r.stdout and "admin@watchover.local" in r.stdout and "Watchover v" in r.stdout
    # second run keeps the secrets, mcp on adds the profile
    run(stage, "update")
    assert f"POSTGRES_PASSWORD={pw}" in (d / ".env").read_text()
    r = run(stage, "mcp", "on")
    assert r.returncode == 0 and "WATCHOVER_MCP=1" in (d / ".env").read_text() and "--profile mcp up -d" in (d / "calls.log").read_text()


def test_usage_and_missing_docker(stage):
    d, env = stage
    r = run(stage, "bogus")
    assert r.returncode == 1 and "install" in r.stdout
    env["PATH"] = "/nonexistent"
    r = subprocess.run([shutil.which("bash"), str(d / "watchover.sh"), "install"], cwd=d, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "Docker is not installed" in r.stderr


def test_edge_on_off(stage):
    """edge on <host>: Caddy profile, host in .env, plain UI port bound to localhost; edge off restores it."""
    d, _ = stage
    run(stage, "install")
    r = run(stage, "edge", "on", "watchover.sirket.local")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "WATCHOVER_EDGE=1" in env and "WATCHOVER_EDGE_HOST=watchover.sirket.local" in env and "WATCHOVER_EDGE_TLS=internal" in env
    hosts = [l for l in env.splitlines() if l.startswith("WATCHOVER_EDGE_HOSTS=")][0]
    assert hosts.startswith("WATCHOVER_EDGE_HOSTS=watchover.sirket.local, ") and hosts.count(",") == 1          # the machine's IP is added
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml" in env and "WATCHOVER_UI_PORT=127.0.0.1" not in env
    assert "https://watchover.sirket.local" in r.stdout and "--profile edge up -d --remove-orphans --force-recreate dashboard edge" in (d / "calls.log").read_text()
    assert (d / "certs").is_dir()
    r = run(stage, "edge", "off")
    env = (d / ".env").read_text()
    assert r.returncode == 0 and "WATCHOVER_EDGE=0" in env and "COMPOSE_FILE=" not in env and "WATCHOVER_UI_PORT=8501" in env
    r = run(stage, "edge")
    assert r.returncode == 1 and "usage" in r.stderr


def test_llm_on_off(stage):
    d, _ = stage
    run(stage, "install")
    r = run(stage, "llm", "on")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "WATCHOVER_LLM=1" in env and "LLM_BASE_URL=http://ollama:11434" in env and "LLM_PROVIDER=ollama" in env
    assert "LLM_MODEL=qwen2.5:7b-instruct" in env and "LLM_EMBED_MODEL=bge-m3" in env
    calls = (d / "calls.log").read_text()
    assert "--profile llm" in calls and "ollama pull qwen2.5:7b-instruct" in calls and "ollama pull bge-m3" in calls and "--profile llm build trainer" in calls
    r = run(stage, "llm", "off")
    assert r.returncode == 0 and "WATCHOVER_LLM=0" in (d / ".env").read_text() and "LLM_BASE_URL=" in (d / ".env").read_text()
    assert run(stage, "llm").returncode == 1


def test_llm_export_and_import(stage):
    d, _ = stage
    run(stage, "install"); run(stage, "llm", "on")
    r = run(stage, "llm", "export", "out.jsonl")
    assert r.returncode == 0 and "watchover.training export" in (d / "calls.log").read_text() and "cp watchover:/tmp/training.jsonl" in (d / "calls.log").read_text()
    m = d / "models" / "watchover-ops"; m.mkdir(parents=True); (m / "Modelfile").write_text("FROM qwen2.5:1.5b-instruct\n")
    r = run(stage, "llm", "import", str(m))
    calls = (d / "calls.log").read_text()
    assert r.returncode == 0, r.stderr
    assert "cp " in calls and "ollama create watchover-ops -f Modelfile" in calls and "LLM_MODEL=watchover-ops" not in (d / ".env").read_text()   # import never switches
    r = run(stage, "llm", "use", "watchover-ops")
    assert r.returncode == 0 and "LLM_MODEL=watchover-ops" in (d / ".env").read_text()
    assert run(stage, "llm", "import", str(d / "nope")).returncode == 1


def test_llm_on_keeps_existing_connection(stage):
    d, _ = stage
    run(stage, "install")
    env = (d / ".env").read_text(); (d / ".env").write_text(env + "LLM_BASE_URL=http://host.docker.internal:11434\nLLM_MODEL=llama3.1\n")
    r = run(stage, "llm", "on")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "LLM_BASE_URL=http://host.docker.internal:11434" in env and "LLM_MODEL=llama3.1" in env and "existing LLM connection kept" in r.stdout
    run(stage, "llm", "off")
    assert "LLM_BASE_URL=http://host.docker.internal:11434" in (d / ".env").read_text()      # off only undoes its own setting


def test_manual_sizing_is_kept(stage):
    d, _ = stage
    run(stage, "install")
    env = (d / ".env").read_text().replace("WATCHOVER_SIZING=auto", "WATCHOVER_SIZING=manual").replace("WATCHOVER_LLM_CPUS=3", "WATCHOVER_LLM_CPUS=2")
    (d / ".env").write_text(env)
    assert run(stage, "start").returncode == 0
    assert "WATCHOVER_LLM_CPUS=2\n" in (d / ".env").read_text() and "WATCHOVER_SIZING=manual" in (d / ".env").read_text()


def test_profile_cpu8_fixes_the_caps_and_keeps_the_llm_connection(stage):
    d, _ = stage
    run(stage, "install")
    env = (d / ".env").read_text() + "LLM_BASE_URL=http://gpu-box:11434\nLLM_MODEL=qwen2.5:14b\n"
    (d / ".env").write_text(env)
    r = run(stage, "profile", "cpu8")
    assert r.returncode == 0, r.stderr
    e = (d / ".env").read_text()
    for kv in ("WATCHOVER_SIZING=manual", "WATCHOVER_CPUS=6", "WATCHOVER_MEMORY=16g", "WATCHOVER_LLM_CPUS=3", "WATCHOVER_LLM_THREADS=3",
               "WATCHOVER_PARSE_WORKERS=3", "WATCHOVER_PG_SHARED_BUFFERS=2GB", "WATCHOVER_TRAIN_CPUS=4", "WATCHOVER_LLM_KEEPALIVE=24h"):
        assert kv + "\n" in e, kv
    assert "LLM_BASE_URL=http://gpu-box:11434" in e and "LLM_MODEL=qwen2.5:14b" in e          # a configured connection is never touched
    assert run(stage, "start").returncode == 0 and "WATCHOVER_CPUS=6\n" in (d / ".env").read_text()   # manual: start keeps the profile
    shown = run(stage, "profile").stdout
    assert "WATCHOVER_LLM_CPUS" in shown and "3" in shown
    assert run(stage, "profile", "auto").returncode == 0 and "WATCHOVER_SIZING=auto" in (d / ".env").read_text()
    assert run(stage, "profile", "nope").returncode != 0


def test_auto_sizing_on_8_cpu_64_gb_equals_the_cpu8_profile(stage):
    d, env = stage
    fake = d / "bin" / "docker"
    fake.write_text(fake.read_text().replace("*NCPU*) echo 10 ;; *MemTotal*) echo 17179869184 ;;", "*NCPU*) echo 8 ;; *MemTotal*) echo 68719476736 ;;"))
    assert run(stage, "install").returncode == 0
    auto = (d / ".env").read_text()
    assert run(stage, "profile", "cpu8").returncode == 0
    fixed = (d / ".env").read_text()
    keys = ("WATCHOVER_CPUS", "WATCHOVER_MEMORY", "WATCHOVER_RECEIVER_CPUS", "WATCHOVER_RECEIVER_MEMORY", "WATCHOVER_PARSE_WORKERS", "WATCHOVER_SPOOL_MAX_MB",
            "WATCHOVER_PG_CPUS", "WATCHOVER_PG_MEMORY", "WATCHOVER_PG_CACHE", "WATCHOVER_LLM_CPUS", "WATCHOVER_LLM_MEMORY", "WATCHOVER_TRAIN_CPUS",
            "WATCHOVER_TRAIN_MEMORY", "WATCHOVER_LLM_KEEPALIVE")
    val = lambda t, k: [l for l in t.splitlines() if l.startswith(k + "=")][0].split("=", 1)[1]   # noqa: E731
    assert {k: val(auto, k) for k in keys} == {k: val(fixed, k) for k in keys}
    assert val(auto, "WATCHOVER_PG_SHARED_BUFFERS") == "2048MB" and val(fixed, "WATCHOVER_PG_SHARED_BUFFERS") == "2GB"


def test_backup_rotation(stage):
    d, _ = stage
    run(stage, "install")
    (d / "backups").mkdir(exist_ok=True)
    for i in range(4):
        f = d / "backups" / f"watchover-2026010{i + 1}-0200.tgz"; f.write_bytes(b"x"); os.utime(f, (1700000000 + i, 1700000000 + i))
    r = run(stage, "backup", "--keep", "2")
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in (d / "backups").glob("watchover-*.tgz"))
    assert len(left) == 2 and "watchover-20260101-0200.tgz" not in left and "watchover-20260102-0200.tgz" not in left


def test_bundle_and_offline_install(stage):
    d, env = stage
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)   # bundle takes the source tree from git when it can
    r = run(stage, "bundle")
    assert r.returncode == 0, r.stderr
    out = d / f"watchover-bundle-{version(d)}.tar"
    assert out.exists()
    names = subprocess.run(["tar", "tf", str(out)], capture_output=True, text=True, check=True).stdout.split()
    assert "MANIFEST" in names and "images.tar.gz" in names
    log = (d / "calls.log").read_text()
    assert f"docker save watchover:{version(d)} watchover-receiver:{version(d)} postgres:16-alpine" in log and "watchover-trainer:" in log and "ollama/ollama:latest" in log
    assert "docker run --rm -v watchover_watchover-ollama:/v:ro" in log      # model volume exported
    assert "run --rm --no-deps -T trainer python" in log                      # base weights cached for offline auto-learning
    (d / "calls.log").write_text("")
    r = run(stage, "unbundle", str(out))
    assert r.returncode == 0, r.stderr
    log = (d / "calls.log").read_text()
    assert "docker load" in log and "WATCHOVER_OFFLINE=1" in (d / ".env").read_text()
    (d / "calls.log").write_text("")
    r = run(stage, "install")
    assert r.returncode == 0, r.stderr
    log = (d / "calls.log").read_text()
    assert "compose build" not in log and "compose up -d" in log            # offline: loaded image, no build
    r = run(stage, "llm", "on")
    assert r.returncode == 0, r.stderr
    log = (d / "calls.log").read_text()
    assert "build trainer" not in log and "ollama pull" not in log         # offline: no downloads either


def version(d):
    import re
    return re.search(r'^version = "(.*)"', (d / "pyproject.toml").read_text(), re.M).group(1)


def test_rhel_prepare_check_mode(tmp_path):
    """--check never changes the host and needs no root: it prints the plan (would run: …) and the host facts."""
    r = subprocess.run(["bash", str(ROOT / "scripts" / "rhel-prepare.sh"), "--check", "--user", "ops"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "host:" in r.stdout and "would run:" in r.stdout and "done." in r.stdout
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "rhel-prepare.sh")]).returncode == 0


def test_upgrade_pulls_over_local_edits(stage):
    """upgrade = git fetch + fast-forward + update; a local edit to a tracked file (what makes 'git pull' refuse) is stashed and put back."""
    d, env = stage
    def g(*a, cwd=d):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("add", "."); g("commit", "-q", "-m", "v1")
    remote = d.parent / "remote.git"; g("clone", "-q", "--bare", str(d), str(remote)); g("remote", "add", "origin", str(remote))
    work = d.parent / "work"; g("clone", "-q", str(remote), str(work))
    (work / "NEWFILE").write_text("x"); g("add", ".", cwd=work); g("commit", "-q", "-m", "v2", cwd=work); g("push", "-q", "origin", "main", cwd=work)
    (d / "docker-compose.yml").write_text((d / "docker-compose.yml").read_text() + "\n# local tweak\n")
    r = run(stage, "upgrade")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (d / "NEWFILE").exists() and "# local tweak" in (d / "docker-compose.yml").read_text()
    log = (d / "calls.log").read_text()
    assert "compose build" in log and "compose up -d" in log
    assert "set aside" in r.stdout and "docker-compose.yml" in r.stdout


def test_edge_mdns_launchagent_and_reannounce(stage):
    """edge on watchover.local: with launchctl + dns-sd on PATH a LaunchAgent plist is written (survives reboot); without launchctl a dns-sd
    process is kept in .edge-mdns.pid and up() (install/update/start) re-announces when that process is gone; edge off removes both."""
    d, env = stage
    home = d / "home"; (home / "Library" / "LaunchAgents").mkdir(parents=True); env = dict(env, HOME=str(home))
    for tool in ("dns-sd", "launchctl"):
        f = d / "bin" / tool; f.write_text("#!/usr/bin/env bash\necho \"$0 $*\" >> \"$FAKE_LOG\"\n[ \"$(basename $0)\" = dns-sd ] && sleep 30\nexit 0\n"); f.chmod(0o755)
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "on", "watchover.local"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    plist = home / "Library" / "LaunchAgents" / "local.watchover.mdns.watchover-local.plist"
    assert plist.exists() and "<string>watchover.local</string>" in plist.read_text() and "launchctl load" in (d / "calls.log").read_text()
    assert "LaunchAgent" in r.stdout
    # no launchd (Linux): a background dns-sd whose pid is remembered, re-created by up() when it died
    (d / "bin" / "launchctl").unlink(); plist.unlink(); (d / ".edge-mdns.pid").write_text("999999\n")
    r = subprocess.run(["bash", str(d / "watchover.sh"), "start"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Bonjour: watchover.local" in r.stdout and (d / ".edge-mdns.pid").read_text().strip() != "999999"
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "off"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and not (d / ".edge-mdns.pid").exists()




def _tools(d, names, body):
    for n in names:
        f = d / "bin" / n; f.write_text("#!/usr/bin/env bash\n" + body); f.chmod(0o755)


def test_mdns_follower_reannounces_when_the_address_changes(stage, tmp_path):
    """The LaunchAgent runs a follower, not dns-sd with a fixed address: when the Mac moves to another network it announces the new
    address within one poll (here 0.2 s) and writes it to the state file that doctor reads."""
    d, env = stage
    home = d / "home"; (home / "Library" / "LaunchAgents").mkdir(parents=True)
    env = dict(env, HOME=str(home), WATCHOVER_MDNS_EVERY="0.2", IPFILE=str(d / "ip"))
    (d / "ip").write_text("192.168.1.20")
    _tools(d, ["dns-sd"], 'echo "dns-sd $*" >> "$FAKE_LOG"; sleep 30\n')
    _tools(d, ["launchctl"], 'echo "launchctl $*" >> "$FAKE_LOG"; exit 0\n')
    _tools(d, ["route"], 'echo "   interface: en7"\n')
    _tools(d, ["ipconfig"], '[ "$2" = en7 ] && cat "$IPFILE"\n')
    _tools(d, ["hostname"], 'exit 1\n')                                        # macOS: no 'hostname -I'
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "on", "watchover.local"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    plist = (home / "Library" / "LaunchAgents" / "local.watchover.mdns.watchover-local.plist").read_text()
    follow = home / "Library" / "Application Support" / "Watchover" / "mdns-follow.sh"
    assert "mdns-follow.sh" in plist and "<string>watchover.local</string>" in plist and follow.exists() and "192.168.1.20" in r.stdout   # en7 = USB-C Ethernet
    state = home / "Library" / "Application Support" / "Watchover" / "local.watchover.mdns.watchover-local.ip"
    p = subprocess.Popen(["bash", str(follow), "watchover.local", "443", str(state), str(d / "bin" / "dns-sd")], env=env, stdout=subprocess.PIPE, text=True)
    try:
        import time
        for _ in range(50):
            if state.exists() and state.read_text().strip() == "192.168.1.20":
                break
            time.sleep(0.1)
        (d / "ip").write_text("10.20.30.40")                                    # the Mac joined another network
        for _ in range(50):
            if state.read_text().strip() == "10.20.30.40":
                break
            time.sleep(0.1)
        assert state.read_text().strip() == "10.20.30.40"
        log = (d / "calls.log").read_text()
        assert "dns-sd -P watchover.local _https._tcp local 443 watchover.local 192.168.1.20" in log and "watchover.local 10.20.30.40" in log
    finally:
        p.terminate(); p.wait(timeout=5)


def test_old_fixed_address_agent_is_replaced_on_start(stage):
    """Installs from before the follower have a plist that runs dns-sd with a fixed address; start / update / upgrade replace it."""
    d, env = stage
    home = d / "home"; la = home / "Library" / "LaunchAgents"; la.mkdir(parents=True); env = dict(env, HOME=str(home))
    _tools(d, ["dns-sd"], 'sleep 30\n'); _tools(d, ["launchctl"], 'echo "launchctl $*" >> "$FAKE_LOG"; exit 0\n')
    (d / ".env").write_text("WATCHOVER_EDGE=1\nWATCHOVER_EDGE_HOST=watchover.local\nWATCHOVER_EDGE_HOSTS=watchover.local, 192.168.1.20\n")
    old = la / "local.watchover.mdns.watchover-local.plist"
    old.write_text("<plist><dict><key>ProgramArguments</key><array><string>/usr/bin/dns-sd</string><string>-P</string><string>192.168.1.20</string></array></dict></plist>")
    r = run((d, env), "start")
    assert r.returncode == 0, r.stderr
    assert "mdns-follow.sh" in old.read_text() and "Bonjour: watchover.local" in r.stdout


def test_linux_announcement_follows_the_address(stage):
    d, env = stage
    _tools(d, ["dns-sd"], 'sleep 30\n')
    (d / ".env").write_text("WATCHOVER_EDGE=1\nWATCHOVER_EDGE_HOST=watchover.local\n")
    p = subprocess.Popen(["sleep", "30"]); (d / ".edge-mdns.pid").write_text(f"{p.pid}\n"); (d / ".edge-mdns.ip").write_text("10.9.9.9\n")
    try:
        r = run(stage, "start")
        assert r.returncode == 0 and "Bonjour: watchover.local" in r.stdout                  # alive but on an old address: re-announced
        assert (d / ".edge-mdns.ip").read_text().strip() != "10.9.9.9"
    finally:
        p.kill()
    subprocess.run(["bash", str(d / "watchover.sh"), "edge", "off"], cwd=d, env=env, capture_output=True, text=True, timeout=60)


DOCTOR_DOCKER = r"""echo "docker $*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose version") echo "Docker Compose version v2.29" ;;
  "info --format") case "$3" in *NCPU*) echo 10 ;; *MemTotal*) echo 17179869184 ;; esac ;;
esac
[[ "$*" == *" ps --format"* ]] && printf 'dashboard running healthy\npostgres running healthy\nreceiver running \nedge running \n'
case "" in
  "info --format") case "$3" in *NCPU*) echo 10 ;; *MemTotal*) echo 17179869184 ;; esac ;;
esac
exit 0
"""


def test_doctor_checks_and_repairs(stage):
    d, env = stage
    (d / "bin" / "docker").write_text("#!/usr/bin/env bash\n" + DOCTOR_DOCKER)
    ip = subprocess.run(["bash", "-c", "hostname -I | awk '{print $1}'"], capture_output=True, text=True).stdout.strip() or "localhost"
    _tools(d, ["dns-sd"], 'sleep 30\n')
    _tools(d, ["curl"], 'echo "curl $*" >> "$FAKE_LOG"; [[ "$*" == *--resolve* ]] && printf 200 || printf 000\n')
    (d / ".env").write_text("WATCHOVER_EDGE=1\nWATCHOVER_EDGE_HOST=watchover.local\nWATCHOVER_EDGE_HTTPS_PORT=443\n")
    _tools(d, ["getent"], 'exit 2\n')                                          # the name does not resolve
    r = run(stage, "doctor")
    assert r.returncode == 1 and "container receiver is up" in r.stdout and "container dashboard is up" in r.stdout and "edge answers https://watchover.local:443" in r.stdout
    assert "watchover.local resolves to nothing" in r.stdout and "Bonjour: watchover.local" in r.stdout and "mDNSResponder" in r.stdout   # re-announced + hint
    _tools(d, ["getent"], f'echo "{ip} watchover.local"\n')
    r = run(stage, "doctor", "--check")
    assert r.returncode == 0, r.stdout
    assert f"watchover.local resolves to {ip} (this machine)" in r.stdout and "all good: https://watchover.local" in r.stdout
    r = run(stage, "help-me")
    assert "doctor [--check]" in r.stdout and "unbundle FILE" in r.stdout                  # usage lists every command
    subprocess.run(["bash", str(d / "watchover.sh"), "edge", "off"], cwd=d, env=env, capture_output=True, text=True, timeout=60)



def test_update_reports_the_running_version(stage):
    """After an update the script says which version the dashboard really runs, so "the update did not arrive" is visible at once."""
    d, env = stage
    ver = subprocess.run(["bash", "-c", "grep -m1 '^version' pyproject.toml | cut -d'\"' -f2"], cwd=d, capture_output=True, text=True).stdout.strip()
    fake = (d / "bin" / "docker").read_text().replace("exit 0\n", f'[[ "$*" == *"exec -T dashboard curl"* ]] && echo \'{{"ok": true, "version": "{ver}"}}\'\nexit 0\n')
    (d / "bin" / "docker").write_text(fake)
    r = run(stage, "update")
    assert r.returncode == 0, r.stderr
    assert f"running v{ver}" in r.stdout and "Cmd+Shift+R" in r.stdout
    (d / "bin" / "docker").write_text(fake.replace(f'"version": "{ver}"', '"version": "1.0.0"'))
    r = run(stage, "update")
    assert "WARNING: this folder is v" in r.stdout and "'1.0.0'" in r.stdout


def test_demo_clean_counts_restarts_then_cleans(stage):
    """demo-clean: --dry-run only counts; the real run restarts the dashboard (the simulator stops, the in-memory window forgets the
    demo hosts) and then deletes inside the container; anything else is a usage error. WATCHOVER_DEMO=1 stays the Docker default."""
    d, _ = stage
    r = run(stage, "demo-clean", "--dry-run")
    assert r.returncode == 0, r.stderr
    calls = (d / "calls.log").read_text()
    assert "compose exec -T dashboard python -m watchover.demo clean --dry-run" in calls and "compose restart" not in calls
    (d / "calls.log").unlink()
    r = run(stage, "demo-clean", "--yes")                                          # (stdin is no terminal here: no question either way)
    assert r.returncode == 0, r.stderr
    calls = (d / "calls.log").read_text().splitlines()
    restart = calls.index("docker compose restart dashboard")
    clean = calls.index("docker compose exec -T dashboard python -m watchover.demo clean")
    assert restart < clean and not any("--dry-run" in c for c in calls)
    assert "demo data removed" in r.stdout
    r = run(stage, "demo-clean", "--everything")
    assert r.returncode != 0 and "usage: ./watchover.sh demo-clean" in r.stderr
    assert "demo-clean [--dry-run]" in run(stage, "help").stdout
    assert "WATCHOVER_DEMO: ${WATCHOVER_DEMO:-1}" in (d / "docker-compose.yml").read_text()
