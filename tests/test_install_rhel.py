"""install-rhel.sh: the one-command RHEL 9 / Podman install, run against stubs on PATH (dnf, rpm, systemctl, podman, the Compose
binary, firewall-cmd, getenforce, curl, ss, ...) and a fake root (WATCHOVER_TEST_ROOT: /etc, /proc, /opt, /usr/local, /var), so the
script's own decisions (preflight verdicts, proxy files, offline bundle, .env, units, firewall, LLM sizing, summary) are checked
without a RHEL host. What only a real RHEL 9 run proves (SELinux labels, netavark, real builds) is listed in docs/KURULUM_DOCKER.md."""
import os
import re
import shutil
import socket
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ("install-rhel.sh", "watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", "docker-compose.otlp.yml",
         "docker-compose.loghot.yml", "docker-compose.logcold.yml", ".env.docker.example", "pyproject.toml")


def copy_tree(dst):
    for f in FILES:
        (dst / f).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT / f, dst / f)
    for d in ("docker", "scripts"):                                            # the edge's config and its certificate script, rhel-prepare.sh
        shutil.copytree(ROOT / d, dst / d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
VER = re.search(r'^version = "(.*)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
EDGE_IMG = re.search(r"^  edge:\n(?:    .*\n)*?    image: (\S+)", (ROOT / "docker-compose.yml").read_text(), re.M).group(1)   # whatever proxy the edge uses

STUBS = {
    "podman": r'''
echo "podman $*" >> "$FAKE_LOG"; S="$FAKE_STATE"; touch "$S/images" "$S/containers"
norm() { local n="${1#docker.io/}"; echo "${n#library/}"; }
case "$1" in
  --version) echo "podman version 5.2.2" ;;
  version) echo "5.2.2" ;;
  info) case "$*" in *CPUs*) echo 8 ;; *MemTotal*) echo "${FAKE_MEM_BYTES:-34359738368}" ;; *NetworkBackend*) echo "${FAKE_NB:-netavark}" ;; esac ;;
  build) prev=""; for a in "$@"; do [ "$prev" = -t ] && norm "$a" >> "$S/images"; prev="$a"; done ;;
  image) case "$2" in exists|inspect) grep -qx "$(norm "$3")" "$S/images" || exit 1 ;; esac ;;
  load) cat > /dev/null; printf '%s\n' "watchover:$FAKE_VER" "watchover-receiver:$FAKE_VER" postgres:16-alpine "$FAKE_EDGE_IMG" watchover-updater:1 alpine:latest >> "$S/images" ;;
  inspect) if [ "$2" = -f ]; then grep -qx "$4" "$S/containers" && echo true || echo false; fi ;;
  ps) [[ "$*" == *--filter* ]] && sed 's/$/|Up 2 minutes (healthy)/' "$S/containers" ;;
  volume) [ "$2" != exists ] || [[ " ${FAKE_VOLUMES:-} " == *" $3 "* ]] || exit 1 ;;
  cp) mkdir -p "$(dirname "$3")"; echo "-----BEGIN CERTIFICATE-----" > "$3" ;;
esac
exit 0''',
    "compose": r'''
echo "compose $*" >> "$FAKE_LOG"; S="$FAKE_STATE"; touch "$S/containers"
add() { grep -qx "$1" "$S/containers" || echo "$1" >> "$S/containers"; }
case " $* " in
  *" version "*) [[ "$*" == *--short* ]] && echo 2.29.7 || echo "Docker Compose version v2.29.7" ;;
  *" ps "*) [[ "$*" == *--format* ]] && echo "dashboard healthy" ;;
  *" config "*) echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
  *" up "*) for c in watchover watchover-postgres watchover-receiver watchover-keyd; do add $c; done
            [[ "$*" != *"--profile edge"* ]] || add watchover-edge; [[ "$*" != *"--profile updater"* ]] || add watchover-updater ;;
esac
exit 0''',
    "dnf": r'''
echo "dnf $*" >> "$FAKE_LOG"
[ -z "${FAKE_DNF_FAIL:-}" ] || { echo "Error: Failed to download metadata for repo 'rhel-9-for-x86_64-baseos-rpms'"; exit 1; }
case " $* " in
  *" repolist "*) echo "rhel-9-for-x86_64-baseos-rpms  Red Hat Enterprise Linux 9 - BaseOS" ;;
  *" install "*|*" upgrade "*) for a in "$@"; do case "$a" in -*|install|upgrade) ;; *) echo "$a" >> "$FAKE_STATE/rpms" ;; esac; done
    if [[ " $* " == *" docker-compose-plugin "* ]]; then mkdir -p "$(dirname "$WATCHOVER_COMPOSE_BIN")"; cp "$FAKE_BIN/compose" "$WATCHOVER_COMPOSE_BIN"; chmod 755 "$WATCHOVER_COMPOSE_BIN"; fi ;;
esac
exit 0''',
    "rpm": r'''
[ "$1" = -q ] || exit 0; shift; for p in "$@"; do grep -qx "$p" "$FAKE_STATE/rpms" 2>/dev/null || exit 1; done; exit 0''',
    "systemctl": r'''
echo "systemctl $*" >> "$FAKE_LOG"; S="$FAKE_STATE/units"; touch "$S"; u="${*: -1}"
case "$1" in
  is-active) case " ${FAKE_ACTIVE-firewalld chronyd podman.socket} " in *" $u "*) exit 0 ;; esac; exit 3 ;;
  is-enabled) grep -qx "$u" "$S" || exit 1 ;;
  enable) for a in "${@:2}"; do [ "$a" = --now ] || grep -qx "$a" "$S" || echo "$a" >> "$S"; done ;;
esac
exit 0''',
    "firewall-cmd": r'''
echo "firewall-cmd $*" >> "$FAKE_LOG"; F="$FAKE_STATE/fw"; touch "$F"
[ -z "${FAKE_NO_FIREWALLD:-}" ] || exit 252
for a in "$@"; do case "$a" in
  --state) echo running ;; --get-default-zone|--get-zone-of-interface=*) echo public ;;
  --query-port=*) grep -qx "${a#*=}" "$F"; exit ;;
  --add-port=*) grep -qx "${a#*=}" "$F" || echo "${a#*=}" >> "$F" ;;
  --remove-port=*) grep -vx "${a#*=}" "$F" > "$F.t" || true; mv "$F.t" "$F" ;;
esac; done; exit 0''',
    "curl": r'''
url=""; for a in "$@"; do case "$a" in http://*|https://*) url="$a" ;; esac; done
echo "curl $url" >> "$FAKE_LOG"; h=${url#*://}; h=${h%%/*}; h=${h%%:*}
[[ "$*" != *--cacert* ]] || echo "curl-tls $url $*" >> "$FAKE_LOG"
for b in ${FAKE_BLOCK:-}; do [ "$h" != "$b" ] || exit "${FAKE_BLOCK_RC:-7}"; done
if [ "$h" = cdn.redhat.com ] && [ -n "${FAKE_RHSM_CA:-}" ] && [[ "$*" != *"--cacert"*"redhat-uep.pem"* ]]; then exit 60; fi   # the CDN's own CA
[[ "$*" != *http_code* ]] || printf 200; exit 0''',
    "ss": r'''
p=$(echo "$*" | sed -n 's/.*sport = :\([0-9]*\).*/\1/p')
for l in ${FAKE_LISTEN:-}; do [ "${l%%:*}" = "$p" ] || continue
  if [[ "$1" == *p* ]]; then echo "LISTEN 0 511 0.0.0.0:$p 0.0.0.0:* users:((\"${l#*:}\",pid=812,fd=4))"; else echo "LISTEN 0 511 0.0.0.0:$p 0.0.0.0:*"; fi
done; exit 0''',
    "ip": r'''
case "$*" in *"addr show"*) echo "2: eth0    inet 10.20.30.40/24 brd 10.20.30.255 scope global eth0"; echo "3: podman0    inet 10.88.0.1/16 scope global podman0" ;;
  *"route show default"*) echo "default via 10.20.30.1 dev eth0 proto dhcp" ;; esac''',
    "hostname": r'''
case "${1:-}" in -f) echo "${FAKE_FQDN-watchover01.firma.local}" ;; -I) echo "10.20.30.40 10.88.0.1" ;; *) echo watchover01 ;; esac''',
    "df": r'''
echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "/dev/mapper/rhel-root 1000000000 100000000 ${FAKE_FREE_KB:-500000000} 10% /"''',
    "getenforce": "echo Enforcing",
    "timedatectl": "echo yes",
    "systemd-detect-virt": '[ -n "${FAKE_VIRT:-}" ] && echo "$FAKE_VIRT" || { echo none; exit 1; }',
    "findmnt": 'case "$*" in *FSTYPE*) echo "${FAKE_FSTYPE:-xfs}" ;; *) echo /dev/mapper/rhel-root ;; esac',
    "lsblk": "echo 0",
    "id": '[ "${1:-}" = -u ] && echo 0 || exec /usr/bin/id "$@"',
}
for name in ("restorecon", "systemd-run", "subscription-manager", "chcon", "sysctl"):
    STUBS[name] = f'echo "{name} $*" >> "$FAKE_LOG"; exit 0'


@pytest.fixture
def rhel(tmp_path):
    src = tmp_path / "home" / f"watchover-{VER}"
    copy_tree(src)
    r = tmp_path / "root"
    for d in ("etc/yum.repos.d", "etc/dnf", "etc/systemd/system", "proc", "var/lib/containers", "run-systemd"):
        (r / d).mkdir(parents=True)
    (r / "etc/os-release").write_text('NAME="Red Hat Enterprise Linux"\nID="rhel"\nVERSION_ID="9.6"\nPRETTY_NAME="Red Hat Enterprise Linux 9.6 (Plow)"\n')
    (r / "proc/meminfo").write_text("MemTotal:       32768000 kB\n")
    (r / "proc/cpuinfo").write_text("flags\t\t: fpu sse sse2 avx avx2\n")
    (r / "etc/yum.repos.d/redhat.repo").write_text(
        "[rhel-9-for-x86_64-baseos-rpms]\nname = BaseOS\nbaseurl = https://cdn.redhat.com/content/dist/rhel9/$releasever/x86_64/baseos/os\nenabled = 1\n\n"
        "[rhel-9-for-x86_64-supplementary-rpms]\nbaseurl = https://cdn.redhat.com/content/dist/rhel9/x\nenabled = 0\n")
    (r / "etc/dnf/dnf.conf").write_text("[main]\ngpgcheck=1\n")
    (r / "etc/environment").write_text("")
    bin_ = tmp_path / "bin"; bin_.mkdir(); state = tmp_path / "state"; state.mkdir()
    for name, body in STUBS.items():
        f = bin_ / name; f.write_text("#!/usr/bin/env bash\n" + body.lstrip("\n") + "\n"); f.chmod(0o755)
    sock = str(tmp_path / "p.sock"); sk = socket.socket(socket.AF_UNIX); sk.bind(sock)
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "no_proxy", "all_proxy")}
    env.update(PATH=f"{bin_}:{env['PATH']}", WATCHOVER_TEST_ROOT=str(r), WATCHOVER_PODMAN_SOCK=sock, WATCHOVER_SYSTEMD_RUN=str(r / "run-systemd"),
               WATCHOVER_COMPOSE_BIN=str(tmp_path / "plugins" / "docker-compose"), WATCHOVER_VERIFY_TRIES="1", WATCHOVER_HTTPS_TRIES="1",
               FAKE_LOG=str(tmp_path / "calls.log"), FAKE_STATE=str(state), FAKE_BIN=str(bin_), FAKE_VER=VER, FAKE_EDGE_IMG=EDGE_IMG)
    return src, r, env


def run(rhel, *args, **extra):
    src, _, env = rhel
    return subprocess.run(["bash", str(src / "install-rhel.sh"), *args], env=dict(env, **extra), capture_output=True, text=True, timeout=300)


def calls(rhel):
    p = Path(rhel[2]["FAKE_LOG"]); return p.read_text() if p.exists() else ""


def envval(dest, key):
    return next((l.split("=", 1)[1] for l in (dest / ".env").read_text().splitlines() if l.startswith(key + "=")), None)


def test_fresh_install_then_rerun_is_idempotent(rhel):
    src, r, env = rhel
    res = run(rhel)
    out = res.stdout
    assert res.returncode == 0, out + res.stderr
    dest = r / "opt/watchover"; log = calls(rhel); state = Path(env["FAKE_STATE"])
    # preflight, then packages through rhel-prepare.sh (podman + only the Compose plugin of Docker's repository)
    assert "İşletim sistemi: Red Hat Enterprise Linux 9.6 (Plow) (desteklenen: 9.6-9.8)" in out and "Erişim: pypi.org" in out and "Erişim: cdn.redhat.com" in out
    assert "dnf -y install podman container-selinux crun openssl" in log and "dnf -y install docker-compose-plugin" in log and "dnf -y install docker-ce" not in log
    # the release folder, the command wrapper, .env with production defaults and generated secrets (0600)
    assert (dest / "watchover.sh").exists() and (dest / ".install-files").exists() and oct((dest / ".env").stat().st_mode)[-3:] == "600"
    wrapper = (r / "usr/local/bin/watchover").read_text()
    assert f'exec sudo -- "{dest}/watchover.sh"' in wrapper and f'exec "{dest}/watchover.sh" "$@"' in wrapper
    for k, v in (("WATCHOVER_ENGINE", "podman"), ("WATCHOVER_PRODUCTION", "1"), ("WATCHOVER_UPDATER", "1"),
                 ("WATCHOVER_EDGE", "1"), ("WATCHOVER_EDGE_HOST", "watchover01.firma.local"), ("WATCHOVER_BACKUP_AT", "02:15"),
                 ("WATCHOVER_BACKUP_KEEP", "14"), ("WATCHOVER_FW_ZONE", "public"), ("WATCHOVER_LLM_MODELS", "qwen2.5:3b-instruct bge-m3"),
                 ("LLM_MODEL", "qwen2.5:3b-instruct")):
        assert envval(dest, k) == v, k
    pw = envval(dest, "POSTGRES_PASSWORD"); assert pw and pw != "change-me"
    assert "watchover01.firma.local, watchover01, 10.20.30.40" in envval(dest, "WATCHOVER_EDGE_HOSTS") and "10.88.0.1" not in envval(dest, "WATCHOVER_EDGE_HOSTS")
    # images by podman build, the edge on with its CA exported, updater, units for boot and the nightly backup
    assert f"podman build --format docker -f Dockerfile -t docker.io/library/watchover:{VER}" in log and "watchover-updater:1" in log
    assert (dest / "certs/watchover-root.crt").exists()
    units = r / "etc/systemd/system"
    assert f"WorkingDirectory={dest}" in (units / "watchover-stack.service").read_text() and "OnCalendar=*-*-* 02:15:00" in (units / "watchover-backup.timer").read_text()
    # firewalld: only what is published (443, 80 redirect, 8600 agents); 8501 is not
    assert set((state / "fw").read_text().split()) == {"443/tcp", "80/tcp", "8600/tcp"}
    # the local LLM goes to the background (32 GB: the 3B chat model), the install never waits for it
    assert "systemd-run --unit=watchover-llm-setup" in log and "llm on" in log
    assert "GET /healthz" in out and "X-Forwarded-Proto" in out and "ssh -L 8443:localhost:443 kullanici@10.20.30.40" in out
    assert envval(dest, "WATCHOVER_EDGE_HOSTS").endswith("localhost")       # the SSH-tunnel name is on the certificate too
    for s in ("https://watchover01.firma.local/", "https://10.20.30.40/", f"{dest}/certs/watchover-root.crt", "Ajanlar paneli", "Gecelik yedek: açık 02:15, son 14 kopya",
              "Açılışta başlama: açık", "Uygulama içi güncelleme: açık", "arka planda kuruluyor: qwen2.5:3b-instruct bge-m3", "production checklist",
              "/var/log/watchover/install-", "PostgreSQL: yalnız 127.0.0.1:5433"):
        assert s in out, s
    assert "Test-Yonetici-Parola-42" not in out and "Test-Yonetici-Parola-42" not in (r / "var/log/watchover").joinpath(next(p.name for p in (r / "var/log/watchover").glob("install-*.log"))).read_text()
    # re-run: a repair, nothing installed again, secrets and firewall unchanged
    Path(env["FAKE_LOG"]).write_text("")
    res = run(rhel)
    assert res.returncode == 0, res.stdout + res.stderr
    log = calls(rhel)
    ilog = max((r / "var/log/watchover").glob("install-*.log"), key=lambda p: p.stat().st_mtime).read_text()
    assert "dnf -y install" not in log and "rhel-prepare.sh --engine podman --no-firewall --no-docker" in ilog
    assert not (r / "etc/sysctl.d/90-watchover.conf").exists() and "sysctl" not in log
    assert envval(dest, "POSTGRES_PASSWORD") == pw and set((state / "fw").read_text().split()) == {"443/tcp", "80/tcp", "8600/tcp"}
    assert "Podman paketleri kurulu: paket deposu gerekmiyor" in res.stdout and "onarıldı / güncel" in res.stdout
    assert (r / "etc/dnf/dnf.conf").read_text() == "[main]\ngpgcheck=1\n" and not (r / "etc/containers/containers.conf.d/watchover-proxy.conf").exists()
    # plain agent ingest off (agents only over HTTPS): 8600 is closed on the next run
    (dest / ".env").write_text((dest / ".env").read_text().replace("WATCHOVER_LIVE_PORT=8600", "WATCHOVER_LIVE_PORT=127.0.0.1:8600"))
    res = run(rhel)
    assert res.returncode == 0 and set((state / "fw").read_text().split()) == {"443/tcp", "80/tcp"} and "8600/tcp kapatıldı" in res.stdout


def test_corporate_proxy_from_etc_environment(rhel):
    src, r, env = rhel
    (r / "etc/environment").write_text('HTTPS_PROXY="http://ops:s3cret@proxy.firma.local:3128"\nNO_PROXY=".firma.local,10.0.0.0/8"\n')
    res = run(rhel)
    assert res.returncode == 0, res.stdout + res.stderr
    dest = r / "opt/watchover"; out = res.stdout
    assert "Kurumsal proxy: http://***@proxy.firma.local:3128" in out and "s3cret" not in out
    assert "proxy=http://ops:s3cret@proxy.firma.local:3128" in (r / "etc/dnf/dnf.conf").read_text()
    cc = (r / "etc/containers/containers.conf.d/watchover-proxy.conf").read_text()
    assert "http_proxy = false" in cc and '"HTTPS_PROXY=http://ops:s3cret@proxy.firma.local:3128"' in cc
    assert oct((r / "etc/containers/containers.conf.d/watchover-proxy.conf").stat().st_mode)[-3:] == "600"   # a password in it
    assert 'Environment="HTTPS_PROXY=http://ops:s3cret@proxy.firma.local:3128"' in (r / "etc/systemd/system/podman.service.d/watchover-proxy.conf").read_text()
    no = envval(dest, "WATCHOVER_NO_PROXY")
    for n in (".firma.local", "10.0.0.0/8", "localhost", "0.0.0.0", "ollama", "postgres", "watchover01.firma.local", "10.20.30.40"):
        assert n in no.split(","), n
    assert envval(dest, "WATCHOVER_HTTPS_PROXY") == "http://ops:s3cret@proxy.firma.local:3128"
    log = calls(rhel)
    assert "--build-arg HTTPS_PROXY=http://ops:s3cret@proxy.firma.local:3128 --build-arg https_proxy=" in log    # apt / pip / npm in the builds
    assert "subscription-manager config --server.proxy_hostname=proxy.firma.local --server.proxy_port=3128" in log
    # the variables of the calling shell win over /etc/environment (sudo hands them over), and a re-run keeps dnf.conf as it is
    res = run(rhel, "--check", HTTPS_PROXY="http://proxy2.firma.local:8080")
    assert res.returncode == 0 and "Kurumsal proxy: http://proxy2.firma.local:8080 (kaynak: ortam" in res.stdout


def make_bundle(src, compose=True, arch="x86_64"):
    stage = src.parent / "bstage"; (stage / "bin").mkdir(parents=True); (stage / "volumes").mkdir(); (stage / "source").mkdir()
    (stage / "MANIFEST").write_text(f"version={VER}\narch={arch}\nimages=watchover:{VER} watchover-receiver:{VER} postgres:16-alpine {EDGE_IMG} "
                                    f"watchover-updater:1 alpine:latest\nvolumes=\ncompose={1 if compose else 0}\n")
    (stage / "images.tar.gz").write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    if compose:
        shutil.copy2(src.parent.parent / "bin" / "compose", stage / "bin" / "docker-compose")
    out = src.parent / f"watchover-bundle-{VER}.tar"
    with tarfile.open(out, "w") as t:
        for n in ("MANIFEST", "bin", "images.tar.gz", "volumes", "source"):
            t.add(stage / n, arcname=n)
    return out


def test_air_gapped_bundle_next_to_the_folder(rhel):
    src, r, env = rhel
    make_bundle(src)
    res = run(rhel, FAKE_BLOCK="registry-1.docker.io pypi.org registry.ollama.ai cdn.redhat.com")   # no internet at all
    out = res.stdout
    assert res.returncode == 1 and "Etkin paket deposu" not in out          # podman still has to come from somewhere: the repo is unreachable
    assert "Erişilemiyor: cdn.redhat.com" in out and "pypi.org" not in out  # only the repository is probed, never the internet
    # a server with Satellite / a local mirror: packages from there, everything else from the bundle
    (r / "etc/yum.repos.d/redhat.repo").write_text("[baseos]\nbaseurl = https://satellite.firma.local/pulp/content/rhel9/baseos\nenabled = 1\n")
    Path(env["FAKE_LOG"]).write_text("")
    res = run(rhel, FAKE_BLOCK="registry-1.docker.io pypi.org registry.ollama.ai cdn.redhat.com")
    out = res.stdout; log = calls(rhel); dest = r / "opt/watchover"
    assert res.returncode == 0, out + res.stderr
    assert "Kapalı ağ paketi: watchover-bundle-" in out and "Compose v2 paketten kuruldu" in out and "Erişim: satellite.firma.local" in out
    assert (r / "usr/local/lib/docker/cli-plugins/docker-compose").exists() and "docker-compose-plugin" not in log
    assert "podman load" in log and "podman build" not in log and envval(dest, "WATCHOVER_OFFLINE") == "1"
    assert not any(h in log for h in ("curl https://pypi.org", "curl https://registry-1.docker.io", "curl https://registry.ollama.ai"))
    assert "kapalı ağ paketinde Ollama" in out and envval(dest, "WATCHOVER_EDGE") == "1"
    assert not (dest / f"watchover-bundle-{VER}.tar").exists()             # the bundle is not copied into the install folder


def test_bundle_for_another_architecture_is_refused(rhel):
    src, r, env = rhel
    make_bundle(src, arch="aarch64")
    res = run(rhel)
    assert res.returncode == 1 and "Paket aarch64 için hazırlanmış, bu sunucu x86_64" in res.stdout


def test_unreachable_host_is_named_and_nothing_changes(rhel):
    src, r, env = rhel
    res = run(rhel, FAKE_BLOCK="files.pythonhosted.org registry.ollama.ai", FAKE_BLOCK_RC="28")
    out = res.stdout
    assert res.returncode == 1
    assert "Erişilemiyor: files.pythonhosted.org (zaman aşımı" in out and "Güvenlik duvarı / proxy ekibine iletin" in out
    assert "    files.pythonhosted.org" in out and "Kurulum başlamadı: 1 sorun" in out          # the LLM registry is a note, not a blocker
    assert "registry.ollama.ai" in out and not (r / "opt/watchover").exists() and "dnf -y install" not in calls(rhel)


def test_os_versions_9_6_to_9_8_older_9_warned_8_and_10_refused(rhel):
    src, r, env = rhel
    (r / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n')
    res = run(rhel)
    assert res.returncode == 1 and "Bu kurulum RHEL 9 içindir" in res.stdout and "Ubuntu 24.04" in res.stdout and calls(rhel).count("dnf") == 0
    (r / "etc/os-release").write_text('ID="rhel"\nVERSION_ID="8.10"\nPRETTY_NAME="Red Hat Enterprise Linux 8.10"\n')
    res = run(rhel)
    assert res.returncode == 1 and "Bu kurulum RHEL 9 içindir (9.6-9.8); bu sunucu Red Hat Enterprise Linux 8.10" in res.stdout
    (r / "etc/os-release").write_text('ID="rhel"\nVERSION_ID="10.0"\nPRETTY_NAME="Red Hat Enterprise Linux 10.0"\n')
    res = run(rhel)
    assert res.returncode == 1 and "RHEL 10 henüz desteklenmiyor" in res.stdout
    (r / "etc/os-release").write_text('ID="rhel"\nVERSION_ID="9.8"\nPRETTY_NAME="Red Hat Enterprise Linux 9.8 (Plow)"\n')
    res = run(rhel, "--check")
    assert res.returncode == 0 and "Red Hat Enterprise Linux 9.8 (Plow) (desteklenen: 9.6-9.8)" in res.stdout
    (r / "etc/os-release").write_text('ID="rocky"\nVERSION_ID="9.1"\nPRETTY_NAME="Rocky Linux 9.1 (Blue Onyx)"\n')
    res = run(rhel, "--check", WATCHOVER_TEST_ARCH="aarch64", FAKE_VIRT="kvm")
    out = res.stdout
    assert res.returncode == 0, out
    assert "Rocky Linux 9.1 (Blue Onyx) · RHEL 9 uyumlu" in out and "hedef RHEL 9.6-9.8 (Podman 5.x). Kurulum sürüyor; önce güncellemeniz önerilir: sudo dnf -y update" in out and "aarch64" in out
    assert "Sanal makinede çalışıyor (kvm)" in out and "NEON" in out and "--check: hiçbir şey değiştirilmedi" in out
    assert not (r / "opt/watchover").exists() and "dnf -y" not in calls(rhel)


def test_low_ram_skips_the_llm_and_a_big_server_gets_7b(rhel):
    src, r, env = rhel
    (r / "proc/meminfo").write_text("MemTotal:        8000000 kB\n")
    (r / "proc/cpuinfo").write_text("flags\t\t: fpu sse sse2\n")
    res = run(rhel)
    out = res.stdout
    assert res.returncode == 0, out + res.stderr
    assert "kurulmadı: RAM 8 GB, yerel LLM için en az 16 GB gerekir" in out and "AVX2 yok" in out and "systemd-run" not in calls(rhel)
    assert "registry.ollama.ai" not in calls(rhel)                         # not even probed below 16 GB
    shutil.rmtree(r / "opt"); (r / "proc/meminfo").write_text("MemTotal:       65536000 kB\n")
    res = run(rhel, WATCHOVER_TEST_ARCH="aarch64")                         # an ARM server (Apple Silicon VM) end to end
    dest = r / "opt/watchover"
    assert res.returncode == 0, res.stdout + res.stderr
    assert "İşlemci mimarisi: aarch64" in res.stdout and envval(dest, "WATCHOVER_LLM_MODELS") == "qwen2.5:7b-instruct qwen2.5:3b-instruct bge-m3"
    assert envval(dest, "LLM_MODEL") == "qwen2.5:7b-instruct" and "systemd-run --unit=watchover-llm-setup" in calls(rhel)


def test_existing_llm_connection_and_nfs_storage(rhel):
    src, r, env = rhel
    dest = r / "opt/watchover"; dest.mkdir(parents=True)
    (dest / ".env").write_text("LLM_BASE_URL=http://llm.firma.local:11434\nLLM_MODEL=llama3.1:70b\nPOSTGRES_PASSWORD=keepme\n"
                               "WATCHOVER_ENGINE_SOCK=/run/user/1000/podman/podman.sock\n")
    res = run(rhel, FAKE_FSTYPE="nfs4")
    assert res.returncode == 0, res.stdout + res.stderr
    # a rootless engine socket: unprivileged ports from 80 up, persisted (rootful, the default, never writes it)
    assert (r / "etc/sysctl.d/90-watchover.conf").read_text().splitlines()[-1] == "net.ipv4.ip_unprivileged_port_start=80" and "sysctl --system" in calls(rhel)
    assert envval(dest, "LLM_BASE_URL") == "http://llm.firma.local:11434" and envval(dest, "LLM_MODEL") == "llama3.1:70b"
    assert envval(dest, "POSTGRES_PASSWORD") == "keepme" and "ağ / paylaşımlı dosya sisteminde (nfs4)" in res.stdout


def test_port_conflict_stops_before_any_change(rhel):
    src, r, env = rhel
    res = run(rhel, FAKE_LISTEN="443:httpd 80:httpd")
    assert res.returncode == 1
    assert "Port 443 (HTTPS) kullanımda: httpd" in res.stdout and "sudo systemctl disable --now httpd" in res.stdout
    assert not (r / "opt/watchover").exists()


def test_watchover_sh_opens_and_closes_feature_ports_as_root(rhel):
    """mcp on / otlp on run with sudo open their ports in firewalld (the installer's zone), off closes them; 127.0.0.1 stays closed."""
    src, r, env = rhel
    assert run(rhel).returncode == 0
    dest = r / "opt/watchover"; fw = Path(env["FAKE_STATE"]) / "fw"
    sh = lambda *a: subprocess.run(["bash", str(dest / "watchover.sh"), *a], env=dict(env, WATCHOVER_ENGINE="podman"), capture_output=True, text=True, timeout=120)
    res = sh("mcp", "on"); assert res.returncode == 0, res.stderr
    assert "8765/tcp" in fw.read_text().split() and "firewalld (public): opened 8765/tcp" in res.stdout
    res = sh("otlp", "on"); assert res.returncode == 0, res.stderr
    assert {"4317/tcp", "4318/tcp"} <= set(fw.read_text().split())
    assert sh("otlp", "off").returncode == 0 and sh("mcp", "off").returncode == 0
    assert set(fw.read_text().split()) == {"443/tcp", "80/tcp", "8600/tcp"}


def test_bundle_carries_the_compose_binary(rhel, tmp_path):
    """./watchover.sh bundle adds this machine's Compose binary (bin/docker-compose) and its architecture to the MANIFEST."""
    src, r, env = rhel
    d = tmp_path / "b"; d.mkdir()
    copy_tree(d)
    plug = tmp_path / "plugins" / "docker-compose"; plug.parent.mkdir(exist_ok=True); shutil.copy2(Path(env["FAKE_BIN"]) / "compose", plug)
    state = Path(env["FAKE_STATE"]); (state / "images").write_text(f"watchover:{VER}\nwatchover-receiver:{VER}\n")
    res = subprocess.run(["bash", str(d / "watchover.sh"), "bundle"], cwd=d, env=dict(env, WATCHOVER_ENGINE="podman"), capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    with tarfile.open(d / f"watchover-bundle-{VER}.tar") as t:
        names = t.getnames(); mf = t.extractfile("MANIFEST").read().decode()
    assert names[0] == "MANIFEST" and "bin/docker-compose" in names
    assert "compose=1" in mf and re.search(r"^arch=(x86_64|aarch64)$", mf, re.M)


def test_upgrade_takes_a_backup_and_removes_stale_files_downgrade_refused(rhel):
    src, r, env = rhel
    assert run(rhel).returncode == 0
    dest = r / "opt/watchover"
    (dest / "old_module.py").write_text("x = 1\n")
    (dest / ".install-files").write_text("\n".join(sorted((dest / ".install-files").read_text().split() + ["./old_module.py"])) + "\n")
    (dest / "pyproject.toml").write_text((dest / "pyproject.toml").read_text().replace(f'version = "{VER}"', 'version = "1.0.0"'))
    pw = envval(dest, "POSTGRES_PASSWORD")
    res = run(rhel)
    assert res.returncode == 0, res.stdout + res.stderr
    ilog = max((r / "var/log/watchover").glob("install-*.log"), key=lambda p: p.stat().st_mtime).read_text()
    assert f"Yükseltme: v1.0.0 → v{VER}" in res.stdout and "W backup --no-logs" in ilog and "yükseltildi (v1.0.0'dan)" in res.stdout
    assert not (dest / "old_module.py").exists() and (dest / "watchover.sh").exists() and envval(dest, "POSTGRES_PASSWORD") == pw
    (dest / "pyproject.toml").write_text((dest / "pyproject.toml").read_text().replace(f'version = "{VER}"', 'version = "99.0"'))
    res = run(rhel)
    assert res.returncode == 1 and "daha yeni bir sürüm kurulu (v99.0" in res.stdout and "Geri al" in res.stdout


def test_red_hat_cdn_is_probed_with_its_own_ca_and_entitlement(rhel):
    """cdn.redhat.com answers with Red Hat's own CA (not in the system store) and wants the entitlement certificate: the probe uses
    what dnf uses (the repository's sslcacert / sslclientcert / sslclientkey), so a registered server is not reported unreachable
    (seen on the first real RHEL 9.8 run)."""
    r = rhel[1]
    for f in ("etc/rhsm/ca/redhat-uep.pem", "etc/pki/entitlement/123.pem", "etc/pki/entitlement/123-key.pem"):
        (r / f).parent.mkdir(parents=True, exist_ok=True); (r / f).write_text("x")
    (r / "etc/yum.repos.d/redhat.repo").write_text(
        "[rhel-9-for-x86_64-baseos-rpms]\nname = BaseOS\nbaseurl = https://cdn.redhat.com/content/dist/rhel9/$releasever/x86_64/baseos/os\nenabled = 1\n"
        "sslverify = 1\nsslcacert = /etc/rhsm/ca/redhat-uep.pem\nsslclientkey = /etc/pki/entitlement/123-key.pem\nsslclientcert = /etc/pki/entitlement/123.pem\n")
    res = run(rhel, "--check", FAKE_RHSM_CA="1")
    out = res.stdout + res.stderr
    assert "Erişim: cdn.redhat.com" in out and "Erişilemiyor: cdn.redhat.com" not in out, out[-2000:]
    tls = [l for l in calls(rhel).splitlines() if l.startswith("curl-tls https://cdn.redhat.com")]
    assert tls and "redhat-uep.pem" in tls[0] and "--cert" in tls[0] and "--key" in tls[0]


def test_ram_is_the_installed_amount_not_memtotal(rhel):
    """A 4096 MB VM (Parallels on Apple Silicon, RHEL 9.8) reads ~3.3 GiB in MemTotal: the kernel and kdump's crashkernel keep the
    rest. It was refused as "3 GB"; the installed amount comes from /proc/iomem, else MemTotal is rounded up (seen on a real run)."""
    r = rhel[1]
    (r / "proc/meminfo").write_text("MemTotal:        3460000 kB\n")                # ~3.3 GiB usable
    res = run(rhel, "--check")
    assert "En az 4 GB RAM" not in res.stdout and "4 GB RAM (kullanılabilir 3.3 GB)" in res.stdout, res.stdout[-1500:]
    (r / "proc/meminfo").write_text("MemTotal:        2900000 kB\n")                # less usable than that: /proc/iomem tells
    (r / "proc/iomem").write_text("00000000-3fffffff : reserved\n40000000-13fffffff : System RAM\n  40210000-41b3ffff : Kernel code\n"
                                  "  dc000000-efffffff : Crash kernel\n")
    res = run(rhel, "--check")
    assert "En az 4 GB RAM" not in res.stdout and "4 GB RAM (kullanılabilir 2.8 GB)" in res.stdout, res.stdout[-1500:]
    (r / "proc/iomem").unlink(); (r / "proc/meminfo").write_text("MemTotal:        1700000 kB\n")   # a 2 GB machine: still refused
    res = run(rhel, "--check")
    assert res.returncode == 1 and "En az 4 GB RAM gerekir (bu sunucu: 2 GB)" in res.stdout
