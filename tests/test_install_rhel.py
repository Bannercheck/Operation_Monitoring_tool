"""install-rhel.sh: the one-command RHEL 9 / Podman install, run against stubs on PATH (dnf, rpm, systemctl, podman, the Compose
binary, firewall-cmd, getenforce, curl, ss, ...) and a fake root (WATCHOVER_TEST_ROOT: /etc, /proc, /opt, /usr/local, /var), so the
script's own decisions (preflight verdicts, proxy files, offline bundle, .env, units, firewall, LLM sizing, summary) are checked
without a RHEL host. What only a real RHEL 9 run proves (SELinux labels, netavark, real builds) is listed in docs/KURULUM_DOCKER.md."""
import hashlib
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
  load) cat > /dev/null; printf '%s\n' "watchover:$FAKE_VER" "watchover-receiver:$FAKE_VER" "watchover-postgres:$FAKE_VER" "$FAKE_EDGE_IMG" watchover-updater:1 alpine:latest >> "$S/images" ;;
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
  *" install "*|*" upgrade "*) for a in "$@"; do case "$a" in -*|install|upgrade) ;; *.rpm) basename "$a" | sed -E 's/-[^-]+-[^-]+\.[^.]+\.rpm$//' >> "$FAKE_STATE/rpms" ;; *) echo "$a" >> "$FAKE_STATE/rpms" ;; esac; done
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
    assert "İşletim sistemi: Red Hat Enterprise Linux 9.6 (Plow) (desteklenen: 8.6+, 9, 10)" in out and "Erişim: pypi.org" in out and "Erişim: cdn.redhat.com" in out
    assert "dnf -y install podman container-selinux crun openssl" in log and "dnf -y install docker-compose-plugin" in log and "dnf -y install docker-ce" not in log
    # the release folder, the command wrapper, .env with production defaults and generated secrets (0600)
    assert (dest / "watchover.sh").exists() and (dest / ".install-files").exists() and oct((dest / ".env").stat().st_mode)[-3:] == "600"
    wrapper = (r / "usr/local/bin/watchover").read_text()
    assert f'exec sudo -- "{dest}/watchover.sh"' in wrapper and f'exec "{dest}/watchover.sh" "$@"' in wrapper
    assert os.readlink(r / "usr/bin/watchover") == "/usr/local/bin/watchover"     # sudo's secure_path has no /usr/local/bin
    for k, v in (("WATCHOVER_ENGINE", "podman"), ("WATCHOVER_PRODUCTION", "1"), ("WATCHOVER_UPDATER", "1"),
                 ("WATCHOVER_EDGE", "1"), ("WATCHOVER_EDGE_HOST", "watchover01.firma.local"), ("WATCHOVER_BACKUP_AT", "02:15"),
                 ("WATCHOVER_BACKUP_KEEP", "14"), ("WATCHOVER_FW_ZONE", "public"), ("WATCHOVER_LLM_MODELS", "qwen2.5:3b-instruct bge-m3"),
                 ("LLM_MODEL", "qwen2.5:3b-instruct")):
        assert envval(dest, k) == v, k
    pw = envval(dest, "POSTGRES_PASSWORD"); assert pw and pw != "change-me"
    assert "watchover01.firma.local, watchover01, 10.20.30.40" in envval(dest, "WATCHOVER_EDGE_HOSTS") and "10.88.0.1" not in envval(dest, "WATCHOVER_EDGE_HOSTS")
    # images by podman build, the edge on with its CA exported, updater, units for boot and the nightly backup
    assert f"podman build --format docker -f Dockerfile -t docker.io/library/watchover:{VER}" in log and "watchover-updater:4" in log
    assert (dest / "certs/watchover-root.crt").exists()
    units = r / "etc/systemd/system"
    assert f"WorkingDirectory={dest}" in (units / "watchover-stack.service").read_text() and "OnCalendar=*-*-* 02:15:00" in (units / "watchover-backup.timer").read_text()
    # firewalld: only what is published (443, 80 redirect, 8600 agents); 8501 is not
    assert set((state / "fw").read_text().split()) == {"443/tcp", "80/tcp", "8600/tcp"}
    # no firewall-cmd --reload (it dropped netavark's runtime rule: container names stopped resolving on RHEL 9.8); repaired once
    assert "firewall-cmd --reload" not in log and "podman network reload --all" in log and "--zone=public --add-port=443/tcp" in log
    # the local LLM goes to the background (32 GB: the 3B chat model), the install never waits for it
    assert "systemd-run --unit=watchover-llm-setup" in log and "llm on" in log
    assert "Sunucu ajanı   (test kökü: atlandı)" in out          # host_agent needs the real containers (rehearsed on a VM)
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
    (stage / "MANIFEST").write_text(f"version={VER}\narch={arch}\nimages=watchover:{VER} watchover-receiver:{VER} watchover-postgres:{VER} {EDGE_IMG} "
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


def test_bundle_too_big_for_the_free_space_stops_before_any_change(rhel, tmp_path):
    """A 15 GB bundle passed the 20 GB check and the load ran out of space half way: the need now comes from the bundle's own
    listing (image archive + Podman's unpacked copy + the loaded layers + the model volumes), checked before anything changes."""
    src, r, env = rhel
    out = make_bundle(src)
    big = tmp_path / "big"; (big / "volumes").mkdir(parents=True)
    with tarfile.open(out) as t:
        t.extractall(big)
    with open(big / "images.tar.gz", "r+b") as f:                     # sparse: the listing says 8 GB, the disk holds nothing
        f.truncate(8 * 1024 ** 3)
    subprocess.run(["tar", "-S", "-cf", str(out), "-C", str(big), "MANIFEST", "bin", "images.tar.gz", "volumes", "source"], check=True)
    res = run(rhel, FAKE_FREE_KB=str(30 * 1048576))                    # 30 GB free: above the old 20 GB line
    assert res.returncode == 1, res.stdout
    assert "Kapalı ağ paketini yüklemek için" in res.stdout and "~55 GB boş alan gerekir (boş: 30 GB)" in res.stdout
    assert not (r / "opt/watchover/watchover.sh").exists() and "podman load" not in calls(rhel)
    res = run(rhel, FAKE_FREE_KB=str(80 * 1048576))
    assert "Paketi yüklemek için yer: ~55 GB gerekir, 80 GB boş" in res.stdout


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


def test_os_versions_8_6_up_to_10_and_older_refused(rhel):
    """RHEL 8.6+, 9.x and 10.x (and the rebuilds) install; RHEL 7, 8.0-8.5 and other systems are refused before any change."""
    src, r, env = rhel
    os_rel = lambda i, v, n: (r / "etc/os-release").write_text(f'ID="{i}"\nVERSION_ID="{v}"\nPRETTY_NAME="{n}"\n')   # noqa: E731
    os_rel("ubuntu", "24.04", "Ubuntu 24.04 LTS")
    res = run(rhel)
    assert res.returncode == 1 and "Bu kurulum Red Hat ailesi içindir" in res.stdout and "Ubuntu 24.04" in res.stdout and calls(rhel).count("dnf") == 0
    os_rel("rhel", "7.9", "Red Hat Enterprise Linux Server 7.9 (Maipo)")
    res = run(rhel)
    assert res.returncode == 1 and "RHEL 7 ve öncesinde Podman 4 ve cgroup v2 yok" in res.stdout
    os_rel("rhel", "8.4", "Red Hat Enterprise Linux 8.4 (Ootpa)")
    res = run(rhel)
    assert res.returncode == 1 and "RHEL 8'de en az 8.6 gerekir" in res.stdout
    for i, v, n, want in (("rhel", "8.10", "Red Hat Enterprise Linux 8.10 (Ootpa)", "(Podman 4.x; desteklenen: 8.6+, 9, 10)"),
                          ("rhel", "9.2", "Red Hat Enterprise Linux 9.2 (Plow)", "9.5+ Podman 5 getirir"),
                          ("rhel", "9.8", "Red Hat Enterprise Linux 9.8 (Plow)", "(desteklenen: 8.6+, 9, 10)"),
                          ("rhel", "10.0", "Red Hat Enterprise Linux 10.0 (Coughlan)", "(desteklenen: 8.6+, 9, 10)"),
                          ("ol", "9.5", "Oracle Linux Server 9.5", "· RHEL 9 uyumlu"),
                          ("almalinux", "10.0", "AlmaLinux 10.0 (Purple Lion)", "· RHEL 10 uyumlu")):
        os_rel(i, v, n)
        res = run(rhel, "--check")
        assert res.returncode == 0 and n in res.stdout and want in res.stdout, res.stdout
    os_rel("rocky", "9.1", "Rocky Linux 9.1 (Blue Onyx)")
    res = run(rhel, "--check", WATCHOVER_TEST_ARCH="aarch64", FAKE_VIRT="kvm")
    out = res.stdout
    assert res.returncode == 0, out
    assert "Rocky Linux 9.1 (Blue Onyx) · RHEL 9 uyumlu" in out and "aarch64" in out
    assert "Sanal makinede çalışıyor (kvm)" in out and "NEON" in out and "--check: hiçbir şey değiştirilmedi" in out
    assert not (r / "opt/watchover").exists() and "dnf -y" not in calls(rhel)


def test_rhel_8_with_podman_4_cgroup_v1_and_cni_dnsname(rhel):
    """RHEL 8.10: Podman 4.9 on cgroup v1 with CNI networking and no netavark in the repositories: the dnsname plugin
    (podman-plugins) resolves the container names, so the install goes on; a Podman older than 4.0 stops it."""
    src, r, env = rhel
    (r / "etc/os-release").write_text('ID="rhel"\nVERSION_ID="8.10"\nPRETTY_NAME="Red Hat Enterprise Linux 8.10 (Ootpa)"\n')
    fake = Path(env["FAKE_BIN"])
    (fake / "podman").write_text((fake / "podman").read_text().replace("5.2.2", "4.9.4"))
    dnf = (fake / "dnf").read_text()
    (fake / "dnf").write_text(dnf.replace('case " $* " in\n  *" repolist "', 'case " $* " in\n  *" netavark "*) echo "No match for argument: netavark"; exit 1 ;;\n  *" repolist "', 1))
    (fake / "stat").write_text('#!/usr/bin/env bash\ncase "$*" in *"%T"*sys/fs/cgroup*) echo tmpfs ;; *) exec /usr/bin/stat "$@" ;; esac\n'); (fake / "stat").chmod(0o755)
    res = run(rhel, FAKE_NB="cni")
    out = res.stdout
    assert res.returncode == 0, out[-3000:]
    assert "cgroup v1 (RHEL 8 varsayılanı): root Podman 4.x ile çalışır" in out and "Podman 4.9.4: çalışır" in out
    assert "konteyner adları dnsname eklentisiyle çözülür" in out and "podman-plugins" in (Path(env["FAKE_STATE"]) / "rpms").read_text()
    assert not (r / "etc/containers/containers.conf.d/watchover-network.conf").exists()      # no switch to a netavark that is not there
    (fake / "podman").write_text((fake / "podman").read_text().replace("4.9.4", "3.4.4"))
    res = run(rhel, FAKE_NB="cni")
    assert res.returncode == 1 and "en az 4.0 gerekir" in res.stdout


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


def build_setup(src, *args, **extra):
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy")}
    env.update(extra)
    out = src.parent / "dist"
    res = subprocess.run(["bash", str(src / "scripts/make-setup.sh"), "--out", str(out), *args], env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    return next(out.glob("watchover-kurulum-*.sh"))


def run_setup(rhel, f, *args, **extra):
    _, _, env = rhel
    return subprocess.run(["bash", str(f), *args], env=dict(env, **extra), capture_output=True, text=True, timeout=300)


def test_one_file_setup_online(rhel, tmp_path):
    """watchover setup: one self-checking file (script + archive); --yes installs without a question, the opened files go away."""
    src, r, env = rhel
    for rel in ("demo/shot.png", "tests/test_x.py", "tests/fixtures/sample.log", "deploy/openshift/Chart.yaml", "AI_JURI.md"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True); (src / rel).write_text("x")
    old = src.parent / "dist" / "watchover-kurulum-0.9.sh"; old.parent.mkdir(exist_ok=True); old.write_text("x")
    keep = src.parent / "dist" / "watchover-kurulum-0.9-x86_64-hepsi-dahil.sh"; keep.write_text("x")
    build_setup(src); f = src.parent / "dist" / f"watchover-kurulum-{VER}.sh"
    assert not old.exists() and keep.exists()                                  # the older online installer goes, the offline one stays
    assert f.name == f"watchover-kurulum-{VER}.sh" and f.read_bytes()[:2] == b"#!" and f.stat().st_size > 65536
    sha = (f.parent / (f.name + ".sha256")).read_text()                       # the whole file's sha256, sha256sum -c format
    assert sha == f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n"
    ex = tmp_path / "ex"; assert run_setup(rhel, f, "--extract", str(ex)).returncode == 0
    tree = ex / f"watchover-{VER}"                                             # the server copy has no demo, tests or chart
    assert (tree / "tests/fixtures/sample.log").exists() and (tree / "install-rhel.sh").exists()
    assert not any((tree / p).exists() for p in ("demo", "tests/test_x.py", "deploy", "AI_JURI.md"))
    work = tmp_path / "work"
    res = run_setup(rhel, f, "--yes", "--workdir", str(work))
    out = res.stdout
    assert res.returncode == 0, out[-3000:] + res.stderr
    assert "sha256 doğru, açıldı" in out and "Ön kontrol" in out and "İşletim sistemi:" in out
    dest = r / "opt/watchover"
    assert (dest / "watchover.sh").exists() and envval(dest, "WATCHOVER_EDGE_HOST") == "watchover01.firma.local"
    assert (dest / "tests/fixtures/sample.log").exists() and not (dest / "demo").exists() and not (dest / "tests/test_x.py").exists()
    assert not (work / f"watchover-kurulum-{VER}").exists()                  # cleaned up
    assert list((r / "var/log/watchover").glob(f"kurulum-{VER}-*.txt"))         # the run is kept in a log
    bad = tmp_path / "bad.sh"; data = bytearray(f.read_bytes()); data[70000] ^= 0xFF; bad.write_bytes(bytes(data))
    res = run_setup(rhel, bad, "--yes", "--workdir", str(work))
    assert res.returncode == 1 and "sha256 tutmuyor" in res.stderr
    short = tmp_path / "short.sh"; short.write_bytes(f.read_bytes()[:100000])
    res = run_setup(rhel, short, "--yes", "--workdir", str(work))
    assert res.returncode == 1 and "Kurulum dosyası eksik" in res.stderr
    res = run_setup(rhel, f, "--check", "--yes", "--workdir", str(work))
    assert res.returncode == 0 and "--check: hiçbir şey kurulmadı" in res.stdout
    res = run_setup(rhel, f, "--yes", "--no-host-agent", "--workdir", str(work))
    assert res.returncode == 0 and "Sunucu ajanı: kurulmadı (--no-host-agent)" in res.stdout


def test_one_file_setup_offline_needs_no_internet_and_no_repository(rhel, tmp_path):
    """--offline: the bundle (images, Compose) and Podman's RPMs are inside; a server that reaches neither the internet nor a
    package repository installs from the file alone. The package is for one CPU architecture."""
    src, r, env = rhel
    bundle = make_bundle(src); moved = tmp_path / bundle.name; bundle.rename(moved)   # only the copy inside the file counts
    rpms = tmp_path / "rpms"; rpms.mkdir()
    for n in ("podman-5.2.2-1.el9", "container-selinux-2.232.1-1.el9", "crun-1.16.1-1.el9", "netavark-1.12.2-1.el9", "aardvark-dns-1.12.2-1.el9",
              "openssl-3.2.2-6.el9", "chrony-4.5-3.el9"):
        (rpms / f"{n}.x86_64.rpm").write_bytes(b"\xed\xab\xee\xdb")
    (rpms / "MANIFEST").write_text("os=el9\narch=x86_64\npackages=podman container-selinux crun netavark aardvark-dns openssl chrony\n")
    f = build_setup(src, "--offline", WATCHOVER_SETUP_BUNDLE=str(moved), WATCHOVER_SETUP_RPMS=str(rpms))
    assert f.name == f"watchover-kurulum-{VER}-x86_64-hepsi-dahil.sh"
    (r / "etc/yum.repos.d/redhat.repo").write_text("")                         # no repository at all
    res = run_setup(rhel, f, "--yes", "--workdir", str(tmp_path / "w"), FAKE_BLOCK="registry-1.docker.io pypi.org registry.ollama.ai cdn.redhat.com")
    out = res.stdout; log = calls(rhel); dest = r / "opt/watchover"
    assert res.returncode == 0, out[-4000:] + res.stderr
    assert "Podman paketleri kurulum dosyasından: 7 RPM (el9, x86_64)" in out and "Kapalı ağ paketi: watchover-bundle-" in out
    assert "--disablerepo=*" in log and "podman build" not in log and "podman load" in log and envval(dest, "WATCHOVER_OFFLINE") == "1"
    assert not any(h in log for h in ("curl https://pypi.org", "curl https://registry-1.docker.io", "curl https://cdn.redhat.com"))
    res = run_setup(rhel, f, "--yes", "--workdir", str(tmp_path / "w"), WATCHOVER_TEST_ARCH="aarch64")
    assert res.returncode == 1 and "Bu paket x86_64 için hazırlanmış, bu sunucu aarch64" in res.stderr


@pytest.mark.skipif(not shutil.which("whiptail"), reason="whiptail (newt) not installed")
def test_one_file_setup_window_wizard(rhel, tmp_path):
    """On a terminal the file is a setup wizard (whiptail): welcome, check, settings, progress bar, result. Driven here through a
    pseudo-terminal by pressing Enter on every screen (the defaults)."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time
    src, r, env = rhel
    f = build_setup(src)
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("bash", ["bash", str(f), "--workdir", str(tmp_path / "w")], dict(env, TERM="xterm", LINES="30", COLUMNS="100"))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    screen, t0, last = b"", time.time(), 0.0
    while time.time() - t0 < 180:
        rd, _, _ = select.select([fd], [], [], 0.3)
        if rd:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            screen += chunk
        if time.time() - last > 1.5:
            try:
                os.write(fd, b"\r"); last = time.time()
            except OSError:
                break
    done, status = os.waitpid(pid, os.WNOHANG)
    if done == 0:                                                              # stuck on a screen: stop it, the assertion shows where
        os.kill(pid, 9); _, status = os.waitpid(pid, 0)
    text = screen.decode("utf-8", "replace")
    assert os.waitstatus_to_exitcode(status) == 0, text[-3000:]
    for want in ("Watchover", "Devam", "Ön kontrol", "Bileşenler", "Kur", "Kurulum tamamlandı"):
        assert want in text, want
    assert (r / "opt/watchover/watchover.sh").exists() and not (tmp_path / "w" / f"watchover-kurulum-{VER}").exists()
    log = next(p for p in (r / "var/log/watchover").glob(f"kurulum-{VER}-*.txt") if not p.name.endswith("-on-kontrol.txt")).read_text()
    assert log.count("▸") >= 8                                                # the bar's steps (STEPS in setup-stub.sh)
