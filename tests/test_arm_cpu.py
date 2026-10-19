"""An Apple M4 under RHEL 9's 5.14 kernel (Parallels VM, aarch64) reports sve2 without sve: the OpenSSL inside `cryptography` ran
SVE2 code and the dashboard / keyd died with exit 132 (v1.58.1). OPENSSL_armcap then names only what the CPU really has; the
Python entry points (watchover/__init__.py) and the nginx edge (docker/nginx/watchover-start.sh) apply the same rule."""
import subprocess
from pathlib import Path

import watchover

ROOT = Path(__file__).resolve().parents[1]
M4_VM = ("fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm jscvt fcma lrcpc dcpop sha3 asimddp "
         "sha512 asimdfhm dit uscat ilrcpc flagm sb dcpodp sve2 flagm2 frint svei8mm")


def test_armcap_only_when_sve2_comes_without_sve():
    assert watchover._armcap(M4_VM) == "0x7d"                                  # NEON, AES, SHA1, SHA256, PMULL, SHA512
    assert watchover._armcap(M4_VM + " sve") is None                            # a real SVE2 CPU (Graviton 3/4): untouched
    assert watchover._armcap("fp asimd aes pmull sha1 sha2") is None            # no SVE at all (M1-M3, Graviton 2)
    assert watchover._armcap("fpu sse sse2 avx avx2") is None and watchover._armcap("") is None
    assert watchover._armcap("asimd sve2") == "0x1"


def _edge_start(tmp_path, features, **env):
    cpu = tmp_path / "cpuinfo"; cpu.write_text(f"processor\t: 0\nFeatures\t: {features}\nCPU implementer\t: 0x41\n")
    entry = tmp_path / "entry.sh"; entry.write_text('#!/bin/sh\necho "armcap=${OPENSSL_armcap:-} args=$*"\n'); entry.chmod(0o755)
    return subprocess.run(["sh", str(ROOT / "docker" / "nginx" / "watchover-start.sh"), "nginx", "-g", "daemon off;"],
                          env={"PATH": "/usr/bin:/bin", "WATCHOVER_CPUINFO": str(cpu), "WATCHOVER_EDGE_ENTRY": str(entry), **env},
                          capture_output=True, text=True, check=True).stdout


def test_edge_start_sets_the_same_armcap_and_keeps_the_command(tmp_path):
    out = _edge_start(tmp_path, M4_VM)
    assert "armcap=0x7d args=nginx -g daemon off;" in out and "SVE2 without SVE" in out
    assert "armcap= args=nginx -g daemon off;" in _edge_start(tmp_path, M4_VM + " sve")
    assert "armcap= args=nginx" in _edge_start(tmp_path, "fp asimd aes")
    assert "armcap=0x1 args=nginx" in _edge_start(tmp_path, M4_VM, OPENSSL_armcap="0x1")   # an operator's own value wins
    compose = (ROOT / "docker-compose.yml").read_text()
    assert 'entrypoint: ["/bin/sh", "/watchover-start.sh"]' in compose and "watchover-start.sh:/watchover-start.sh:ro,z" in compose
    assert 'command: ["nginx", "-g", "daemon off;"]' in compose
