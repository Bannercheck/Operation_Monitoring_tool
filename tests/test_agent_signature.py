"""The agent installers' Ed25519 check of the signed release on every OpenSSL: OpenSSL 3's command line, libcrypto through Python
(OpenSSL 1.1.1, RHEL 8: its command line cannot verify Ed25519 and the signed install stopped there), plain Python (RFC 8032; OpenSSL
older than 1.1.1 or none), and "cannot check" when there is no Python either. boot.sh and install.sh carry the same code."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("openssl") or b"-rawin" not in subprocess.run(
    ["openssl", "pkeyutl", "-help"], capture_output=True).stderr + subprocess.run(["openssl", "pkeyutl", "-help"], capture_output=True).stdout,
    reason="needs OpenSSL 3 to make the test signatures")


def block(script: str) -> str:
    lines, out, on = (ROOT / "scripts" / script).read_text().splitlines(), [], False
    for i, line in enumerate(lines):
        on = on or line.startswith("pybin()")
        if on:
            out.append(line)
            if line == "PYEOF":
                out.append(lines[i + 1])
                return "\n".join(out) + "\n"
    raise AssertionError(f"no verifier in {script}")


@pytest.fixture
def keys(tmp_path):
    (tmp_path / "funcs.sh").write_text(block("agent-boot.sh"))
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(tmp_path / "k.pem")], check=True, capture_output=True)
    der = subprocess.run(["openssl", "pkey", "-in", str(tmp_path / "k.pem"), "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout
    (tmp_path / "k.raw").write_bytes(der[-32:])
    (tmp_path / "msg").write_bytes(b'wo-release-v1\n{"version":"0.8.0"}')
    (tmp_path / "bad").write_bytes(b'wo-release-v1\n{"version":"9.9.9"}')
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(tmp_path / "k.pem"), "-rawin", "-in", str(tmp_path / "msg"),
                    "-out", str(tmp_path / "sig")], check=True)
    old = tmp_path / "old"; old.mkdir()                                  # an OpenSSL 1.1.1 command line: no -rawin, dgst works
    real = shutil.which("openssl")
    (old / "openssl").write_text(f'#!/bin/sh\n[ "$1 $2" = "pkeyutl -help" ] && {{ echo "Usage: pkeyutl [options]" >&2; exit 1; }}\nexec {real} "$@"\n')
    (old / "openssl").chmod(0o755)
    return tmp_path


def verify(d: Path, key="k.raw", sig="sig", msg="msg", path=None, **env) -> int:
    e = dict(os.environ, **env)
    if path is not None:
        e["PATH"] = path
    r = subprocess.run(["bash", "-c", f"set -euo pipefail; source funcs.sh; rc=0; ed25519_verify {key} {sig} {msg} || rc=$?; exit $rc"],
                       cwd=d, env=e, capture_output=True, text=True)
    return r.returncode


def test_both_installers_carry_the_same_verifier():
    assert block("agent-boot.sh") == block("agent-install.sh")


def test_openssl_3_command_line(keys):
    assert verify(keys) == 0 and verify(keys, msg="bad") == 1


def test_openssl_1_1_1_goes_through_libcrypto(keys):
    path = f"{keys / 'old'}:{os.environ['PATH']}"
    assert verify(keys, path=path) == 0 and verify(keys, msg="bad", path=path) == 1


def test_plain_python_rfc8032(keys):
    path = f"{keys / 'old'}:{os.environ['PATH']}"
    assert verify(keys, path=path, WO_ED25519_PURE="1") == 0 and verify(keys, msg="bad", path=path, WO_ED25519_PURE="1") == 1
    vectors = [("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
                "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
               ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
                "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")]
    for i, (k, m, s) in enumerate(vectors):                              # RFC 8032 §7.1, tests 1 and 2
        (keys / f"v{i}.key").write_bytes(bytes.fromhex(k)); (keys / f"v{i}.msg").write_bytes(bytes.fromhex(m)); (keys / f"v{i}.sig").write_bytes(bytes.fromhex(s))
        assert verify(keys, key=f"v{i}.key", sig=f"v{i}.sig", msg=f"v{i}.msg", path=path, WO_ED25519_PURE="1") == 0
    assert verify(keys, key="v1.key", sig="v0.sig", msg="v1.msg", path=path, WO_ED25519_PURE="1") == 1


def test_nothing_to_check_with_says_so(keys):
    bare = keys / "bare"; bare.mkdir()
    for b in ("bash", "cat", "grep", "printf"):
        (bare / b).symlink_to(shutil.which(b))
    assert verify(keys, path=str(bare)) == 2
