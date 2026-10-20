"""Watchover: universal SRE ingestion and correlation engine (deterministic core, optional LLM enrichment)."""

__version__ = "1.59.4"

import os as _os

_ARMCAP_BITS = {"asimd": 0x1, "aes": 0x4, "sha1": 0x8, "sha2": 0x10, "pmull": 0x20, "sha512": 0x40}   # OpenSSL arm_arch.h


def _armcap(features: str):
    """OPENSSL_armcap for a CPU whose kernel reports SVE2 without SVE, else None. An Apple M4 under RHEL 9's 5.14 kernel (a
    Parallels / VMware VM) shows sve2 (from SME) but no sve; the OpenSSL inside `cryptography` then runs SVE2 code and the
    process dies with SIGILL (exit 132: dashboard and keyd kept restarting). The value names only what the CPU really has
    (NEON, AES, SHA1/2/512, PMULL), so that path is never taken."""
    f = set(features.split())
    if "sve2" not in f or "sve" in f:
        return None
    return hex(sum(v for k, v in _ARMCAP_BITS.items() if k in f))


def _openssl_cpu_guard() -> None:
    """Set before any OpenSSL is loaded (this package is imported first by every entry point); children inherit it."""
    if _os.environ.get("OPENSSL_armcap") or not _os.path.exists("/proc/cpuinfo"):
        return
    try:
        if _os.uname().machine not in ("aarch64", "arm64"):
            return
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            line = next((ln for ln in fh if ln.startswith("Features")), "")
    except (OSError, AttributeError):
        return
    cap = _armcap(line.partition(":")[2])
    if cap:
        _os.environ["OPENSSL_armcap"] = cap


_openssl_cpu_guard()
