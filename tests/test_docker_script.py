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
    for f in ("watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", "docker-compose.loghot.yml", "docker-compose.logcold.yml",
              ".env.docker.example", "pyproject.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / "scripts").mkdir(exist_ok=True); shutil.copy(ROOT / "scripts" / "edge-cert.sh", tmp_path / "scripts" / "edge-cert.sh"); shutil.copy(ROOT / "scripts" / "lb-config.sh", tmp_path / "scripts" / "lb-config.sh")
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
                 "WATCHOVER_LLM_CPUS": "5", "WATCHOVER_LLM_THREADS": "5", "WATCHOVER_LLM_MEMORY": "4g", "WATCHOVER_TRAIN_CPUS": "5", "WATCHOVER_TRAIN_MEMORY": "4g",
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
    """edge on <host>: the nginx edge with a certificate from the install's own CA (names + the machine's IP), host in .env, plain
    UI port no longer published; a load balancer is trusted with edge lb; a company certificate is kept; edge off restores it."""
    d, _ = stage
    run(stage, "install")
    r = run(stage, "edge", "on", "watchover.sirket.local")
    assert r.returncode == 0, r.stderr
    env = (d / ".env").read_text()
    assert "WATCHOVER_EDGE=1" in env and "WATCHOVER_EDGE_HOST=watchover.sirket.local" in env and "WATCHOVER_EDGE_TLS=internal" in env
    hosts = [l for l in env.splitlines() if l.startswith("WATCHOVER_EDGE_HOSTS=")][0]
    assert hosts.startswith("WATCHOVER_EDGE_HOSTS=watchover.sirket.local, ") and hosts.count(",") == 1          # the machine's IP is added
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml" in env and "WATCHOVER_UI_PORT=127.0.0.1" not in env
    assert "https://watchover.sirket.local" in r.stdout and "--profile edge up -d --remove-orphans --force-recreate dashboard receiver edge" in (d / "calls.log").read_text()
    assert "WATCHOVER_EDGE_SERVER_NAMES=watchover.sirket.local " in env
    if shutil.which("openssl"):
        assert (d / "certs" / "watchover.crt").is_file() and (d / "certs" / "watchover-root.crt").is_file() and (d / "certs" / ".ca" / "watchover-ca.key").is_file()
        san = subprocess.run(["openssl", "x509", "-in", str(d / "certs" / "watchover.crt"), "-noout", "-text"], capture_output=True, text=True).stdout
        assert "DNS:watchover.sirket.local" in san and "IP Address:" in san
        assert "sha256" in run(stage, "edge", "cert").stdout
    r = run(stage, "edge", "lb", "10.0.0.5/32", "10.0.1.0/24")
    assert r.returncode == 0 and "WATCHOVER_EDGE_TRUSTED=10.0.0.5/32 10.0.1.0/24" in (d / ".env").read_text() and "/healthz" in r.stdout
    assert run(stage, "edge", "lb", "10.0.0.5;reboot").returncode != 0
    r = run(stage, "edge", "lb", "off")
    assert r.returncode == 0 and "WATCHOVER_EDGE_TRUSTED=\n" in (d / ".env").read_text() + "\n"
    r = run(stage, "edge", "off")
    env = (d / ".env").read_text()
    assert r.returncode == 0 and "WATCHOVER_EDGE=0" in env and "COMPOSE_FILE=" not in env and "WATCHOVER_UI_PORT=8501" in env
    r = run(stage, "edge")
    assert r.returncode == 1 and "usage" in r.stderr


def test_edge_lbconf_prints_the_load_balancer_configuration(stage):
    """edge lbconf: F5 BIG-IP / NetScaler / HAProxy snippets for the public name with this server's address and ports: the
    health check on /healthz (its JSON is {"ok":true,...}), X-Forwarded-For, TLS to the server, 300 s idle (v1.59)."""
    d, _ = stage
    run(stage, "install")
    assert run(stage, "edge", "lbconf").returncode != 0                                    # no public name yet
    run(stage, "edge", "public", "watchover.sirket.test", "10.20.0.10/32")
    r = run(stage, "edge", "lbconf", "all", "10.20.0.100")
    out = r.stdout
    assert r.returncode == 0, r.stderr
    for part in ("F5 BIG-IP", "NetScaler", "HAProxy", "destination 10.20.0.100:443", "insert-xforwarded-for enabled", 'recv "\\"ok\\": *true"',
                 '-recv "\\"ok\\":true"', "-cip ENABLED X-Forwarded-For", "option forwardfor", "option httpchk GET /healthz", "idle-timeout 300",
                 "sni str(watchover.sirket.test)", "_sys_https_redirect"):
        assert part in out, part
    assert ":443" in out and "watchover_pool" in out
    assert "edge lbconf f5|netscaler|haproxy" in run(stage, "edge", "public", "watchover.sirket.test").stdout
    for part in ("no-tlsv1 no-tlsv1.1", "TLS13-AES128-GCM-SHA256", "cipher-group watchover_tls", "-tls13 ENABLED -denySSLReneg ALL",
                 "TLS1.3-AES256-GCM-SHA384", "ssl-min-ver TLSv1.2 ciphers ECDHE-", "ciphersuites TLS_AES_128_GCM_SHA256"):   # TLS 1.2 + 1.3 only (v1.59.5)
        assert part in out, part
    assert "$tls1" not in out and "CBC" not in out
    bad = subprocess.run(["sh", str(ROOT / "scripts" / "lb-config.sh"), "f5", "a;reboot", "10.0.0.1"], capture_output=True, text=True)
    assert bad.returncode == 2 and subprocess.run(["sh", str(ROOT / "scripts" / "lb-config.sh"), "cisco", "a.b", "10.0.0.1"]).returncode == 2


def test_edge_tls_sets_the_versions(stage):
    """edge tls 1.2|1.3: TLS 1.2 + 1.3 (default) or TLS 1.3 only on the edge, kept in .env (WATCHOVER_EDGE_TLS_MIN)."""
    d, _ = stage
    run(stage, "install")
    assert "1.2 + 1.3" in run(stage, "edge", "tls").stdout
    r = run(stage, "edge", "tls", "1.3")
    assert r.returncode == 0 and "WATCHOVER_EDGE_TLS_MIN=1.3" in (d / ".env").read_text() and "TLS 1.3 only" in r.stdout
    assert "1.3 only" in run(stage, "edge", "tls").stdout
    assert run(stage, "edge", "tls", "1.0").returncode != 0 and "WATCHOVER_EDGE_TLS_MIN=1.3" in (d / ".env").read_text()
    run(stage, "edge", "tls", "1.2")
    assert "WATCHOVER_EDGE_TLS_MIN=1.2" in (d / ".env").read_text()
    assert "WATCHOVER_EDGE_TLS_MIN: ${WATCHOVER_EDGE_TLS_MIN:-1.2}" in (ROOT / "docker-compose.yml").read_text()


def test_edge_lbtest_rehearses_the_load_balancer(stage):
    """edge lbtest: an HAProxy container from 'edge lbconf haproxy' joins the edge's network on 9443 / 9080, is trusted for the
    test, and the checks run: healthz through it, http -> https, the client's address (not the LB's) in the edge's log with a
    forged X-Forwarded-For ignored; stop removes it and restores the trusted list (v1.59.2)."""
    d, env = stage
    fake = d / "bin" / "docker"
    fake.write_text(fake.read_text().replace('echo "docker $*" >> "$FAKE_LOG"\n', 'echo "docker $*" >> "$FAKE_LOG"\n'
        '[[ "$*" == "inspect watchover-edge"* ]] && { echo "watchover_default "; exit 0; }\n'
        '[[ "$*" == "inspect watchover-lbtest"* ]] && { echo "10.89.0.50 "; exit 0; }\n'
        '[[ "$*" == "logs --tail 300 watchover-edge"* ]] && { echo "${FAKE_SEEN:-10.89.0.1} - - [x] \\"GET /api/health?lbtest=tok1 HTTP/1.1\\" 200"; exit 0; }\n', 1))
    _tools(d, ["curl"], 'echo "curl $*" >> "$FAKE_LOG"; case "$*" in *%{redirect_url}*) printf "301 https://watchover.sirket.test/" ;; *http_code*) printf 200 ;; esac\n')
    run(stage, "install")
    run(stage, "edge", "public", "watchover.sirket.test", "10.20.0.10/32")
    e = dict(env, WATCHOVER_LBTEST_TOKEN="tok1", WATCHOVER_LBTEST_WAIT="1")
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "lbtest"], cwd=d, env=e, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (d / "calls.log").read_text()
    assert "run -d --name watchover-lbtest --user 0 --network watchover_default -p 9443:9443 -p 9080:9080" in calls
    cfg = (d / ".lbtest" / "haproxy.cfg").read_text()
    assert "bind *:9443 ssl crt /etc/haproxy/certs/watchover.sirket.test.pem" in cfg and "server watchover1 edge:443 ssl verify required" in cfg
    assert "resolvers podman init-addr last,libc,none check check-sni watchover.sirket.test" in cfg and "option forwardfor" in cfg
    assert oct((d / ".lbtest" / "front.pem").stat().st_mode)[-3:] == "600"
    assert "WATCHOVER_EDGE_TRUSTED=10.20.0.10/32 10.89.0.50/32" in (d / ".env").read_text()
    for part in ("/healthz through the LB -> 200", "http on the LB -> 301 https://", "the edge logs the client (10.89.0.1), not the LB (10.89.0.50)",
                 "the load balancer path works"):
        assert part in r.stdout, part
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "lbtest"], cwd=d, env=dict(e, FAKE_SEEN="203.0.113.66"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "a forged X-Forwarded-For was believed" in r.stdout
    r = subprocess.run(["bash", str(d / "watchover.sh"), "edge", "lbtest"], cwd=d, env=dict(e, FAKE_SEEN="10.89.0.50"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "X-Forwarded-For was not used" in r.stdout
    assert "WATCHOVER_EDGE_TRUSTED=10.20.0.10/32 10.89.0.50/32\n" in (d / ".env").read_text()           # not added twice
    r = run(stage, "edge", "lbtest", "stop")
    assert r.returncode == 0 and "WATCHOVER_EDGE_TRUSTED=10.20.0.10/32\n" in (d / ".env").read_text() and not (d / ".lbtest").exists()
    assert "rm -f watchover-lbtest" in (d / "calls.log").read_text()


def test_env_comment_on_an_empty_value_is_cleaned(stage):
    """An older .env.docker.example had "WATCHOVER_EDGE_TRUSTED=   # load balancers ... (./watchover.sh edge lb)": Docker Compose
    reads that comment as the value, nginx got "/" as a trusted proxy and would not start. Every write_env cleans such lines."""
    d, _ = stage
    run(stage, "install")
    env = (d / ".env").read_text()
    (d / ".env").write_text(env.replace("WATCHOVER_EDGE_TRUSTED=\n", "WATCHOVER_EDGE_TRUSTED=           # load balancers / CIDRs (./watchover.sh edge lb)\n")
                            + "WATCHOVER_X=  # a comment\nWATCHOVER_Y=value  # kept\n")
    assert run(stage, "start").returncode == 0
    env = (d / ".env").read_text()
    assert "WATCHOVER_EDGE_TRUSTED=\n" in env and "WATCHOVER_X=\n" in env and "WATCHOVER_Y=value  # kept" in env
    assert oct((d / ".env").stat().st_mode)[-3:] == "600"
    assert all(not l.split("=", 1)[1].lstrip().startswith("#") for l in (ROOT / ".env.docker.example").read_text().splitlines()
               if l and not l.startswith("#") and "=" in l)
    assert run(stage, "edge", "lb", "/").returncode != 0 and run(stage, "edge", "lb", "10.0.0").returncode != 0


def _ossl(*a, **k):
    return subprocess.run(["openssl", *[str(x) for x in a]], capture_output=True, text=True, check=k.get("check", True))


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_edge_public_company_certificate_and_check(stage, tmp_path):
    """edge public NAME LB: the LB's name first on the certificate, the public URL for links, the LB trusted, the network team's
    list. edge csr / company: a request for the company CA, then what it issued (DER + a .p7b that holds the leaf again) installed:
    leaf found by its key, chain kept for agents, the own CA's marker gone; another name or another key is refused. edge check."""
    d, _ = stage
    run(stage, "install")
    name = "watchover.sirket.test"
    r = run(stage, "edge", "public", name, "10.20.0.10/32")
    assert r.returncode == 0, r.stdout + r.stderr
    env = (d / ".env").read_text()
    assert f"WATCHOVER_PUBLIC_URL=https://{name}" in env and "WATCHOVER_EDGE_TRUSTED=10.20.0.10/32" in env and f"WATCHOVER_EDGE_HOST={name}" in env
    assert "VIP:443/tcp" in r.stdout and "GET /healthz" in r.stdout and f"edge csr {name}" in r.stdout
    assert f"DNS:{name}" in _ossl("x509", "-in", d / "certs" / "watchover.crt", "-noout", "-text").stdout
    assert run(stage, "edge", "public", "bad;name").returncode != 0 and run(stage, "edge", "public", name, "10.0.0.1;x").returncode != 0
    r = run(stage, "edge", "csr")
    key = d / "certs" / ".company" / "watchover.key"
    assert r.returncode == 0 and (d / "certs" / "watchover.csr").is_file() and oct(key.stat().st_mode)[-3:] == "600", r.stdout + r.stderr
    req = _ossl("req", "-in", d / "certs" / "watchover.csr", "-noout", "-text").stdout
    assert f"DNS:{name}" in req and "localhost" not in req
    ca = tmp_path / "ca"; ca.mkdir()
    _ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca / "ca.key", "-out", ca / "ca.crt", "-days", "30", "-subj", "/CN=Sirket Root CA",
          "-addext", "basicConstraints=critical,CA:true", "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    def issue(csr, dns, out):
        (ca / "ext.cnf").write_text(f"subjectAltName=DNS:{dns}\nextendedKeyUsage=serverAuth\n")
        _ossl("x509", "-req", "-in", csr, "-CA", ca / "ca.crt", "-CAkey", ca / "ca.key", "-CAcreateserial", "-days", "30", "-extfile", ca / "ext.cnf", "-out", out)
    issue(d / "certs" / "watchover.csr", name, ca / "issued.pem")
    _ossl("x509", "-in", ca / "issued.pem", "-outform", "der", "-out", ca / "issued.cer")
    _ossl("crl2pkcs7", "-nocrl", "-certfile", ca / "ca.crt", "-certfile", ca / "issued.pem", "-out", ca / "chain.p7b")
    r = run(stage, "edge", "company", ca / "issued.cer", ca / "chain.p7b")
    assert r.returncode == 0 and "company certificate installed" in r.stdout, r.stdout + r.stderr
    assert "WATCHOVER_EDGE_TLS=company" in (d / ".env").read_text() and not (d / "certs" / ".ca" / "issued").exists()
    crt = d / "certs" / "watchover.crt"
    assert crt.read_text().count("BEGIN CERTIFICATE") == 2 and name in _ossl("x509", "-in", crt, "-noout", "-subject").stdout
    assert _ossl("verify", "-CAfile", ca / "ca.crt", crt).returncode == 0
    assert _ossl("pkey", "-in", d / "certs" / "watchover.key", "-pubout").stdout == _ossl("x509", "-in", crt, "-noout", "-pubkey").stdout
    assert "Sirket Root CA" in _ossl("x509", "-in", d / "certs" / "watchover-ca.crt", "-noout", "-subject").stdout   # agents pin it
    assert "company certificate" in subprocess.run(["sh", str(d / "scripts" / "edge-cert.sh"), str(d / "certs"), name], capture_output=True, text=True).stdout
    before = crt.read_text()
    issue(d / "certs" / "watchover.csr", "other.sirket.test", ca / "other.pem")                  # another name: refused, nothing changed
    r = run(stage, "edge", "company", ca / "other.pem", ca / "ca.crt")
    assert r.returncode != 0 and f"does not name {name}" in r.stderr and crt.read_text() == before
    _ossl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", ca / "x.key", "-subj", f"/CN={name}", "-out", ca / "x.csr")
    issue(ca / "x.csr", name, ca / "x.pem")                                                       # another key: refused
    r = run(stage, "edge", "company", ca / "x.pem")
    assert r.returncode != 0 and "belongs to the key" in r.stderr and crt.read_text() == before
    r = run(stage, "edge", "check")                                                               # no DNS record, nothing on :443 here
    assert r.returncode == 1 and f"names {name}" in r.stdout and "does not resolve" in r.stdout and "trusted load balancers: 10.20.0.10/32" in r.stdout


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


def test_llm_on_picks_the_chat_model_by_measured_speed(stage):
    """llm on measures the bundled Ollama (watchover.llmbench auto) and writes the model this machine carries: the 14B gets a
    16 GB memory share; a model chosen by hand is never replaced; WATCHOVER_LLM_AUTO=0 turns it off (v1.59)."""
    d, env = stage
    fake = d / "bin" / "docker"
    fake.write_text(fake.read_text().replace("*MemTotal*) echo 17179869184 ;;", "*MemTotal*) echo 68719476736 ;;").replace('echo "docker $*" >> "$FAKE_LOG"\n',
        'echo "docker $*" >> "$FAKE_LOG"\n[[ "$*" == *"watchover.llmbench auto"* ]] && { echo "CHAT=${FAKE_CHAT:-qwen2.5:14b-instruct}"; '
        'echo "SUMMARY=7b 12.1 tok/s, 14b 6.2 tok/s"; echo "NOTE=measured"; exit 0; }\n', 1))
    run(stage, "install")
    r = run(stage, "llm", "on")
    assert r.returncode == 0, r.stderr
    e = (d / ".env").read_text(); calls = (d / "calls.log").read_text()
    assert "LLM_MODEL=qwen2.5:14b-instruct" in e and "WATCHOVER_LLM_MEMORY=16g" in e and "WATCHOVER_LLM_BENCH=7b 12.1 tok/s, 14b 6.2 tok/s (" in e
    assert "python -m watchover.llmbench auto --mem-gb 64 --threads 5 --min-tps 5 --floor-tps 2.5" in calls and "local LLM: qwen2.5:14b-instruct" in r.stdout
    assert "up -d --no-deps ollama dashboard" in calls
    run(stage, "llm", "use", "watchover-ops")                                   # chosen by hand: auto leaves it
    r = subprocess.run(["bash", str(d / "watchover.sh"), "llm", "auto"], cwd=d, env=dict(env, FAKE_CHAT="qwen2.5:7b-instruct"), capture_output=True, text=True, timeout=60)
    assert "chosen by you: not changed" in r.stdout and "LLM_MODEL=watchover-ops" in (d / ".env").read_text()
    run(stage, "llm", "use", "qwen2.5:14b-instruct")
    r = subprocess.run(["bash", str(d / "watchover.sh"), "llm", "auto"], cwd=d, env=dict(env, FAKE_CHAT="qwen2.5:7b-instruct"), capture_output=True, text=True, timeout=60)
    e = (d / ".env").read_text()
    assert "LLM_MODEL=qwen2.5:7b-instruct" in e and "WATCHOVER_LLM_MEMORY=10g" in e                # back to the 7B: the share follows
    (d / ".env").write_text(e + "WATCHOVER_LLM_AUTO=0\n")
    assert "llm auto off" in run(stage, "llm", "on").stdout


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
    env = (d / ".env").read_text().replace("WATCHOVER_SIZING=auto", "WATCHOVER_SIZING=manual").replace("WATCHOVER_LLM_CPUS=5", "WATCHOVER_LLM_CPUS=2")
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
    for kv in ("WATCHOVER_SIZING=manual", "WATCHOVER_CPUS=6", "WATCHOVER_MEMORY=16g", "WATCHOVER_LLM_CPUS=4", "WATCHOVER_LLM_THREADS=4",
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


def test_keyd_has_its_own_healthcheck():
    """keyd runs from the dashboard image, whose HEALTHCHECK asks :8501: keyd was always "unhealthy" (RHEL 9.8 VM)."""
    c = (ROOT / "docker-compose.yml").read_text()
    keyd = c[c.index("\n  keyd:"):c.index("\n  receiver:")] if c.index("\n  keyd:") < c.index("\n  receiver:") else c[c.index("\n  keyd:"):]
    assert "healthcheck:" in keyd and "8650" in keyd.split("healthcheck:", 1)[1].split("volumes:", 1)[0]


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
    assert "container names resolve (postgres" in r.stdout
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
    demo hosts) and then deletes inside the container; anything else is a usage error. There is no demo switch any more."""
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
    assert "WATCHOVER_DEMO" not in (d / "docker-compose.yml").read_text()


def test_reset_data_counts_requests_at_start_and_reports(stage):
    """reset-data: the counts first; without a terminal it needs --yes; the reset is requested for the dashboard's next start (nothing
    in memory can write back), the dashboard restarts, the result is printed."""
    d, _ = stage
    r = run(stage, "reset-data", "--dry-run")
    assert r.returncode == 0, r.stderr
    calls = (d / "calls.log").read_text()
    assert "compose exec -T dashboard python -m watchover.demo reset --dry-run" in calls and "compose restart" not in calls
    (d / "calls.log").unlink()
    r = run(stage, "reset-data")                                                   # stdin is no terminal: refused without --yes
    assert r.returncode != 0 and "--yes" in r.stderr and "--at-start" not in (d / "calls.log").read_text()
    (d / "calls.log").unlink()
    r = run(stage, "reset-data", "--yes")
    assert r.returncode == 0, r.stderr
    calls = (d / "calls.log").read_text().splitlines()
    req = calls.index("docker compose exec -T dashboard python -m watchover.demo reset --yes --at-start")
    restart = calls.index("docker compose restart dashboard")
    res = calls.index("docker compose exec -T dashboard python -m watchover.demo reset --result")
    assert req < restart < res and "analysis data reset" in r.stdout
    assert run(stage, "reset-data", "--all").returncode != 0
    assert "reset-data [--dry-run] [--yes]" in run(stage, "help").stdout


# ---- RHEL server: separate log disks, Podman, production install, nightly backup, start on boot
SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
mkdir -p "$FAKE_STATE"
case "$1" in
  enable)     for u in "$@"; do case "$u" in *.service|*.timer) touch "$FAKE_STATE/$u" ;; esac; done ;;
  disable)    for u in "$@"; do case "$u" in *.service|*.timer) rm -f "$FAKE_STATE/$u" ;; esac; done ;;
  is-enabled) for u in "$@"; do case "$u" in *.service|*.timer) [ -f "$FAKE_STATE/$u" ] && exit 0 || exit 1 ;; esac; done ;;
esac
exit 0
"""
CRONTAB = r"""#!/usr/bin/env bash
case "$1" in -l) [ -f "$FAKE_CRON" ] && cat "$FAKE_CRON" || exit 1 ;; -) t=$(cat); printf '%s\n' "$t" > "$FAKE_CRON" ;; esac
"""
PODMAN = r"""#!/usr/bin/env bash
echo "podman $*" >> "$FAKE_LOG"
case "$1 $2" in
  "info --format") case "$3" in *CPUs*) echo 8 ;; *MemTotal*) echo 68719476736 ;; esac ;;
esac
[ "$1" = "--version" ] && echo "podman version 5.2.2"
exit 0
"""
COMPOSE_BIN = r"""#!/usr/bin/env bash
echo "compose[$DOCKER_HOST] $*" >> "$FAKE_LOG"
case "$1" in
  version) echo "Docker Compose version v2.29.7" ;;
  ps) [[ "$*" == *--format* ]] && echo "dashboard healthy" ;;
  config) echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
esac
exit 0
"""


@pytest.fixture
def server(stage, tmp_path):
    """stage + fake systemd (units in a folder, enable/disable state) and a fake crontab"""
    d, env = stage
    for name, body in (("systemctl", SYSTEMCTL), ("crontab", CRONTAB)):
        f = d / "bin" / name; f.write_text(body); f.chmod(0o755)
    (tmp_path / "run-systemd").mkdir(); (tmp_path / "units").mkdir()
    env.update(WATCHOVER_SYSTEMD_RUN=str(tmp_path / "run-systemd"), WATCHOVER_SYSTEMD_DIR=str(tmp_path / "units"),
               FAKE_STATE=str(tmp_path / "systemd-state"), FAKE_CRON=str(tmp_path / "crontab.txt"))
    return d, env


def envval(d, key):
    return next((l.split("=", 1)[1] for l in (d / ".env").read_text().splitlines() if l.startswith(key + "=")), None)


def test_separate_hot_and_cold_log_disks(stage, tmp_path):
    """WATCHOVER_LOG_HOT_PATH / _COLD_PATH: host directories bind-mounted at /logs/hot|cold through COMPOSE_FILE, the ownership is
    checked with a message that says what to run; empty paths keep the data volume (no COMPOSE_FILE at all)."""
    d, env = stage
    assert run(stage, "install").returncode == 0
    assert envval(d, "WATCHOVER_LOG_HOT_PATH") == "" and envval(d, "COMPOSE_FILE") is None          # default: inside the data volume
    hot, cold = tmp_path / "disk1" / "hot", tmp_path / "disk2" / "cold"
    hot.mkdir(parents=True); cold.mkdir(parents=True); hot.chmod(0o755); cold.chmod(0o755)
    text = (d / ".env").read_text().replace("WATCHOVER_LOG_HOT_PATH=\n", f"WATCHOVER_LOG_HOT_PATH={hot}\n").replace("WATCHOVER_LOG_COLD_PATH=\n", f"WATCHOVER_LOG_COLD_PATH={cold}\n")
    (d / ".env").write_text(text)
    if os.geteuid() == 0 or os.getegid() == 0:                     # mode 755 owned by root/us: not writable for UID 10001 / GID 0
        r = run(stage, "start")
        assert r.returncode == 1 and f"sudo chown -R 10001:0 {hot}" in r.stderr and "chmod 2770" in r.stderr
    hot.chmod(0o2777 if os.getegid() != 0 else 0o2770); cold.chmod(0o777 if os.getegid() != 0 else 0o2770)
    r = run(stage, "start")
    assert r.returncode == 0, r.stderr
    assert envval(d, "COMPOSE_FILE") == "docker-compose.yml:docker-compose.loghot.yml:docker-compose.logcold.yml"
    r = run(stage, "edge", "on", "watchover.firma.com")                                           # edge and log disks together
    assert envval(d, "COMPOSE_FILE") == "docker-compose.yml:docker-compose.edge.yml:docker-compose.loghot.yml:docker-compose.logcold.yml"
    r = run(stage, "storage")                                                                      # = storage show: read-only, in the image
    calls = (d / "calls.log").read_text()
    assert r.returncode == 0 and "suggest" not in r.stdout.lower() and "hot_max_gb ~" not in r.stdout                    # nothing is proposed
    assert (f"run --rm --network none --user 0 -v watchover_watchover-data:/data:ro -v {hot}:/hot:ro,z -v {cold}:/cold:ro,z watchover:"
            in calls and f"python -m watchover.logtiers show --home /data --hot /hot --cold /cold --hot-path {hot} --cold-path {cold}" in calls)
    r = run(stage, "edge", "off")
    assert envval(d, "COMPOSE_FILE") == "docker-compose.yml:docker-compose.loghot.yml:docker-compose.logcold.yml"
    (d / ".env").write_text((d / ".env").read_text().replace(f"WATCHOVER_LOG_COLD_PATH={cold}", "WATCHOVER_LOG_COLD_PATH=relative/cold"))
    r = run(stage, "start")
    assert r.returncode == 1 and "must be an absolute host directory" in r.stderr
    (d / ".env").write_text((d / ".env").read_text().replace("WATCHOVER_LOG_COLD_PATH=relative/cold", "WATCHOVER_LOG_COLD_PATH=").replace(f"WATCHOVER_LOG_HOT_PATH={hot}", "WATCHOVER_LOG_HOT_PATH="))
    assert run(stage, "start").returncode == 0 and envval(d, "COMPOSE_FILE") is None
    if os.geteuid() == 0:                                                                          # root creates a missing directory itself
        new = tmp_path / "disk3" / "hot"
        (d / ".env").write_text((d / ".env").read_text().replace("WATCHOVER_LOG_HOT_PATH=\n", f"WATCHOVER_LOG_HOT_PATH={new}\n"))
        r = run(stage, "start")
        assert r.returncode == 0 and new.is_dir() and new.stat().st_uid == 10001 and oct(new.stat().st_mode)[-4:] == "2770", r.stderr
    for k in ("hot", "cold"):                                                                      # what the override files mount
        y = (d / f"docker-compose.log{k}.yml").read_text()
        assert f"${{WATCHOVER_LOG_{k.upper()}_PATH:?" in y and f":/logs/{k}:z" in y and f"WATCHOVER_LOG_{k.upper()}_DIR: /logs/{k}" in y
        assert all(f"  {svc}: *log-{k}" in y for svc in ("dashboard", "mcp", "legacy"))


def test_compose_files_merge_with_the_real_compose(tmp_path):
    """the real Compose (when installed) accepts the base file with every override: log disks bind-mounted with :z next to /data,
    the edge resets the dashboard's plain port, MCP forwards live tools to the dashboard; bind mounts carry the SELinux label"""
    import json
    ngx = (ROOT / "docker" / "nginx" / "watchover.conf.template").read_text()                    # edge: /mcp -> MCP container, rest -> API
    assert "location ~ ^/mcp(/|$)" in ngx and "set $wo_mcp mcp:8765;" in ngx and "set $wo_ui watchover:8501;" in ngx
    real = shutil.which("docker")
    if not real or subprocess.run([real, "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose not installed")
    for f in ("docker-compose.yml", "docker-compose.edge.yml", "docker-compose.loghot.yml", "docker-compose.logcold.yml", ".env.docker.example"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / ".env").write_text((ROOT / ".env.docker.example").read_text() + "WATCHOVER_LOG_HOT_PATH=/srv/wo/hot\nWATCHOVER_LOG_COLD_PATH=/srv/wo/cold\n"
                                   "COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml:docker-compose.loghot.yml:docker-compose.logcold.yml\n")
    out = subprocess.run([real, "compose", "--profile", "mcp", "--profile", "legacy", "--profile", "edge", "config", "--format", "json"],
                         cwd=tmp_path, capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "COMPOSE_FILE"})
    assert out.returncode == 0, out.stderr
    svc = json.loads(out.stdout)["services"]
    for name in ("dashboard", "mcp", "legacy"):
        mounts = {v["target"]: v for v in svc[name]["volumes"]}
        assert mounts["/logs/hot"]["source"] == "/srv/wo/hot" and mounts["/logs/hot"]["bind"]["selinux"] == "z"
        assert mounts["/logs/cold"]["source"] == "/srv/wo/cold" and mounts["/data"]["source"] == "watchover-data"
        assert svc[name]["environment"]["WATCHOVER_LOG_HOT_DIR"] == "/logs/hot" and svc[name]["environment"]["WATCHOVER_LOG_COLD_DIR"] == "/logs/cold"
    assert "/logs/hot" not in {v["target"] for v in svc["receiver"]["volumes"]}
    assert not svc["dashboard"].get("ports")                                                       # edge on: the interface over https only
    assert svc["mcp"]["environment"]["WATCHOVER_API_URL"] == "http://watchover:8501"               # live tools forwarded to the dashboard
    assert all(v.get("bind", {}).get("selinux") == "z" for v in svc["edge"]["volumes"] if v["type"] == "bind")
    (tmp_path / ".env").write_text((ROOT / ".env.docker.example").read_text() + "COMPOSE_FILE=docker-compose.yml:docker-compose.loghot.yml\n")
    out = subprocess.run([real, "compose", "config"], cwd=tmp_path, capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "COMPOSE_FILE"})
    assert out.returncode != 0 and "WATCHOVER_LOG_HOT_PATH must name a host directory" in out.stderr       # override without a path: clear error


def test_podman_engine_socket_compose_and_native_build(server, tmp_path):
    """WATCHOVER_ENGINE=podman: rootful Podman through podman.socket with the Compose v2 binary (DOCKER_HOST), images built by podman
    build under Docker's names, sizing from podman info, multi-image save, helper containers fully qualified with :z, and the stack
    starts on boot by itself (watchover-stack.service with ExecStop: no daemon restarts Podman containers)."""
    d, env = server
    import socket
    for name, body in (("podman", PODMAN), ("docker-compose", COMPOSE_BIN)):
        f = d / "bin" / name; f.write_text(body); f.chmod(0o755)
    sock_path = str(tmp_path / "p.sock"); sk = socket.socket(socket.AF_UNIX); sk.bind(sock_path)
    env.update(WATCHOVER_ENGINE="podman", WATCHOVER_PODMAN_SOCK=sock_path, WATCHOVER_COMPOSE_BIN=str(d / "bin" / "docker-compose"))
    r = run(server, "install")
    assert r.returncode == 0, r.stderr
    log = (d / "calls.log").read_text(); v = version(d)
    assert f"podman build --format docker -f Dockerfile -t docker.io/library/watchover:{v} --build-arg GIT_REV=" in log and f"--build-arg VERSION={v} ." in log
    assert f"podman build --format docker -f Dockerfile.receiver -t docker.io/library/watchover-receiver:{v} ." in log
    assert f"compose[unix://{sock_path}] up -d --remove-orphans" in log and "docker compose" not in log
    assert "compose build" not in log.replace("podman build", "")                                     # no BuildKit on the socket
    assert envval(d, "WATCHOVER_ENGINE") == "podman" and envval(d, "WATCHOVER_CPUS") == "6" and envval(d, "WATCHOVER_MEMORY") == "16g"
    unit = (tmp_path / "units" / "watchover-stack.service").read_text()
    assert "After=network-online.target podman.socket" in unit and f'ExecStart="{d}/watchover.sh" start' in unit and f'ExecStop="{d}/watchover.sh" stop' in unit
    assert "Environment=WATCHOVER_ENGINE=podman" in unit and "systemctl enable watchover-stack.service" in (d / "calls.log").read_text()
    (d / "calls.log").write_text("")
    assert run(server, "backup", "--no-logs").returncode == 0
    log = (d / "calls.log").read_text()
    assert ":/out:z docker.io/library/alpine:latest tar czf /out/data.tgz --exclude=./logs -C /data ." in log and "podman run --rm -v watchover_watchover-data:/data" in log
    assert run(server, "save", str(tmp_path / "img.tgz")).returncode == 0 and f"podman save -m watchover:{v} watchover-receiver:{v}" in (d / "calls.log").read_text()
    # detection: no WATCHOVER_ENGINE and a 'docker' that is podman-docker -> podman
    del env["WATCHOVER_ENGINE"]; (d / ".env").write_text((d / ".env").read_text().replace("WATCHOVER_ENGINE=podman", "WATCHOVER_ENGINE="))
    (d / "bin" / "docker").write_text('#!/usr/bin/env bash\n[ "$1" = --version ] && echo "podman version 5.2.2"\nexit 0\n')
    assert run(server, "start").returncode == 0 and envval(d, "WATCHOVER_ENGINE") == "podman"
    # the socket is missing / not ours: clear messages
    env["WATCHOVER_PODMAN_SOCK"] = str(tmp_path / "nope.sock")
    r = run(server, "start")
    assert r.returncode == 1 and "systemctl enable --now podman.socket" in r.stderr
    env["WATCHOVER_ENGINE"] = "lxc"
    r = run(server, "start")
    assert r.returncode == 1 and "WATCHOVER_ENGINE must be docker or podman" in r.stderr


def test_production_install_prints_the_checklist(server):
    """install --production: production mode and the go-live checklist (backup timer, start on boot, admin password,
    disk caps, log disks, TLS, firewall); 'checklist' prints it again with what is done ticked."""
    d, _ = server
    r = run(server, "install", "--production")
    assert r.returncode == 0, r.stderr
    assert envval(d, "WATCHOVER_PRODUCTION") == "1" and not envval(d, "WATCHOVER_DEMO")
    out = r.stdout
    assert "production checklist" in out and "[x] real data only: the simulator is retired" in out and "[ ] nightly backup: off" in out
    assert "./watchover.sh backup-timer on" in out and "admin@watchover.local" in out and "hot_max_gb ~ " not in out
    assert "[ ] raw-log paths set by hand (hot: not set, archive: not set): ./watchover.sh storage set --hot DIR --cold DIR" in out
    assert "archive kept 365 days" in out and "edge on" in out and "8765" in out
    assert run(server, "backup-timer", "on").returncode == 0
    out = run(server, "checklist").stdout
    assert "[x] nightly backup: on (systemd, 02:15)" in out
    r = run(server, "install", "--prod-ish")
    assert r.returncode == 1 and "usage: ./watchover.sh install [--production]" in r.stderr
    assert "install --production" in run(server, "help").stdout


def test_backup_timer_systemd_and_cron(server, tmp_path):
    """backup-timer on [HH:MM]: a systemd service + timer (Persistent, backup --no-logs) where systemd runs, a tagged crontab line
    elsewhere; off removes exactly that, other cron lines stay."""
    d, env = server
    assert run(server, "install").returncode == 0
    ino = (d / ".env").stat().st_ino
    r = run(server, "backup-timer", "on", "03:30")
    assert r.returncode == 0, r.stderr
    assert (d / ".env").stat().st_ino == ino and oct((d / ".env").stat().st_mode)[-3:] == "600"   # rewritten in place: sudo keeps its owner
    svc = (tmp_path / "units" / "watchover-backup.service").read_text(); tmr = (tmp_path / "units" / "watchover-backup.timer").read_text()
    assert f'ExecStart="{d}/watchover.sh" backup --no-logs' in svc and f"WorkingDirectory={d}" in svc and "After=docker.service" in svc
    assert "OnCalendar=*-*-* 03:30:00" in tmr and "Persistent=true" in tmr and "WantedBy=timers.target" in tmr
    log = (d / "calls.log").read_text()
    assert "systemctl daemon-reload" in log and "systemctl enable --now watchover-backup.timer" in log and envval(d, "WATCHOVER_BACKUP_AT") == "03:30"
    assert "on (systemd, 03:30)" in run(server, "backup-timer").stdout
    assert run(server, "backup-timer", "on", "25:00").returncode == 1
    r = run(server, "backup-timer", "off")
    assert r.returncode == 0 and not (tmp_path / "units" / "watchover-backup.timer").exists() and "disable --now watchover-backup.timer" in (d / "calls.log").read_text()
    assert "nightly backup: off" in run(server, "backup-timer").stdout
    # no systemd: cron
    env["WATCHOVER_SYSTEMD_RUN"] = str(tmp_path / "no-systemd")
    (tmp_path / "crontab.txt").write_text("0 1 * * * /usr/local/bin/other-job\n")
    assert run(server, "backup-timer", "on").returncode == 0                                       # the time set before is kept
    cron = (tmp_path / "crontab.txt").read_text()
    assert "0 1 * * * /usr/local/bin/other-job" in cron and f"30 3 * * * cd '{d}' && ./watchover.sh backup --no-logs >> 'backups/backup.log' 2>&1 # watchover-backup" in cron
    assert run(server, "backup-timer", "on", "04:05").returncode == 0                                # idempotent: one line, new time
    cron = (tmp_path / "crontab.txt").read_text()
    assert cron.count("# watchover-backup") == 1 and "5 4 * * * cd" in cron
    assert run(server, "backup-timer", "off").returncode == 0
    assert (tmp_path / "crontab.txt").read_text().strip() == "0 1 * * * /usr/local/bin/other-job"


def test_backup_without_logs_and_a_restore_that_keeps_them(stage):
    """backup --no-logs leaves /data/logs out (MANIFEST logs=0); restoring such a backup wipes everything but the raw logs"""
    import tarfile
    d, _ = stage
    assert run(stage, "install").returncode == 0
    (d / ".env").write_text((d / ".env").read_text().replace("WATCHOVER_BACKUP_DIR=\n", f"WATCHOVER_BACKUP_DIR={d / 'safe'}\n"))
    r = run(stage, "backup", "--no-logs", "--keep", "3")
    assert r.returncode == 0, r.stderr
    log = (d / "calls.log").read_text()
    assert "-v watchover_watchover-data:/data" in log and "tar czf /out/data.tgz --exclude=./logs -C /data ." in log
    out = next((d / "safe").glob("watchover-*.tgz"))
    with tarfile.open(out) as t:
        assert "logs=0" in t.extractfile("./MANIFEST").read().decode()
    assert oct(out.stat().st_mode)[-3:] == "600"
    assert run(stage, "backup", "--bogus").returncode == 1
    (d / "calls.log").write_text("")
    r = run(stage, "restore", str(out))
    assert r.returncode == 0, r.stderr
    assert "find /data -mindepth 1 -maxdepth 1 ! -name logs -exec rm -rf {} + && tar xzf /in/data.tgz -C /data" in (d / "calls.log").read_text()


def test_rhel_prepare_podman_check_mode():
    """--engine podman: Podman from the RHEL repositories, only the Compose plugin, podman.socket; --mcp adds 8765 to the firewall"""
    r = subprocess.run(["bash", str(ROOT / "scripts" / "rhel-prepare.sh"), "--check", "--engine", "podman", "--mcp", "--user", "ops"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "would run: dnf -y install podman container-selinux" in out and "would run: systemctl enable --now podman.socket" in out
    assert "8765/tcp" in out and "engine: podman" in out and "dnf -y remove podman" not in out and "docker-ce docker-ce-cli" not in out
    r = subprocess.run(["bash", str(ROOT / "scripts" / "rhel-prepare.sh"), "--check", "--engine", "lxc"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "--engine must be docker or podman" in r.stderr


def test_admin_password_reads_it_inside_the_container(stage):
    d, _ = stage
    r = run(stage, "admin-password")
    assert r.returncode == 0, r.stderr
    assert "compose exec -T dashboard python -m watchover.useradmin initial-password" in (d / "calls.log").read_text()
    assert "admin-password" in run(stage, "help").stdout
