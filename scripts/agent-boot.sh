#!/usr/bin/env bash
# Watchover agent bootstrap: the one-line install command from the Agents panel downloads this file, checks its sha256 (in the
# command) and runs it; it verifies the installer before anything of it runs, then hands over every argument:
#   f=$(mktemp) && curl -fsSL -o "$f" http://<dashboard>:8600/agent/boot.sh && echo "<sha256>  $f" | sha256sum -c --quiet \
#     && sudo bash "$f" --url http://<dashboard>:8600/ingest --release-key SHA256:<fingerprint> --enroll-key wk_… --env prod
#   --release-key SHA256:…   the dashboard's release key (keyd): the signed manifest must verify against it (OpenSSL 3, or python3 / RHEL 8's platform-python with OpenSSL 1.1.1)
#   --ca-sha256 <sha256>     https with the edge's own CA: /agent/ca.crt is fetched and accepted only with this sha256, then
#                            trusted for everything that follows and installed for the agent (no manual CA copying)
# The installer's sha256 comes from the signed manifest (or, without --release-key, from an https answer verified by TLS).
# Kept small and stable on purpose: the command carries this file's sha256, so commands already handed out stay valid
# across Watchover upgrades and agent self-updates as long as this file does not change.
set -euo pipefail
URL=""; RKEY=""; CASHA=""; PASS=()
while [[ $# -gt 0 ]]; do case "$1" in
  --url) URL="$2"; PASS+=("$1" "$2"); shift;;
  --release-key) RKEY="$2"; PASS+=("$1" "$2"); shift;;
  --ca-sha256) CASHA="$2"; shift;;
  *) PASS+=("$1");; esac; shift; done
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }
[[ -n "$URL" ]] || die "--url is required"
[[ -z "$CASHA" || "$CASHA" =~ ^[0-9a-f]{64}$ ]] || die "--ca-sha256 must be 64 lowercase hex characters"
command -v curl >/dev/null 2>&1 || die "curl is required"
BASE="${URL%/}"; BASE="${BASE%/ingest}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
C=(curl -fsSL --max-time 120)
jget(){ sed -n "s/.*\"$2\": *\"\([^\"]*\)\".*/\1/p" "$1" | head -n1; }
# Ed25519 check of the signed release. OpenSSL 3's command line does it (pkeyutl -rawin); OpenSSL 1.1.1 (RHEL 8) has Ed25519 in
# the library but its command line cannot verify it, so there libcrypto is called through Python's ctypes (python3, or RHEL 8's
# /usr/libexec/platform-python that dnf itself needs); OpenSSL older than 1.1.1 (RHEL 7: 1.0.2) has no Ed25519 at all, there
# the same Python checks it in plain code (RFC 8032, Python 2.7 / 3). ed25519_verify: 0 valid, 1 invalid, 2 no Python.
pybin() { command -v python3 2>/dev/null || { [[ -x /usr/libexec/platform-python ]] && echo /usr/libexec/platform-python; } || true; }
key_fp() {   # RAW_KEY_FILE -> SHA256:<base64> (empty: neither openssl nor python)
  if command -v openssl >/dev/null 2>&1; then printf 'SHA256:%s' "$(openssl dgst -sha256 -binary "$1" | base64 | tr -d '=\n')"; return 0; fi
  local py; py="$(pybin)"; [[ -n "$py" ]] || return 0
  "$py" -c 'import base64, hashlib, sys; sys.stdout.write("SHA256:" + base64.b64encode(hashlib.sha256(open(sys.argv[1], "rb").read()).digest()).decode().rstrip("="))' "$1"
}
ed25519_verify() {   # RAW_KEY_FILE SIG_FILE MSG_FILE
  local py
  if command -v openssl >/dev/null 2>&1 && openssl pkeyutl -help 2>&1 | grep -q -- '-rawin'; then
    { printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'; cat "$1"; } > "$1.der"
    openssl pkeyutl -verify -pubin -keyform DER -inkey "$1.der" -rawin -in "$3" -sigfile "$2" >/dev/null 2>&1 && return 0 || return 1
  fi
  py="$(pybin)"; [[ -n "$py" ]] || return 2
  "$py" - "$1" "$2" "$3" <<'PYEOF'
import binascii, ctypes, ctypes.util, hashlib, os, sys
lib = None
for n in (ctypes.util.find_library("crypto"), "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so"):
    if not n:
        continue
    try:
        lib = ctypes.CDLL(n)
        break
    except OSError:
        pass
key, sig, msg = [open(f, "rb").read() for f in sys.argv[1:4]]


def via_libcrypto():   # OpenSSL >= 1.1.1: None when this libcrypto has no Ed25519
    if lib is None or not hasattr(lib, "EVP_DigestVerify") or not hasattr(lib, "EVP_PKEY_new_raw_public_key"):
        return None
    V, P, N = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t
    lib.EVP_PKEY_new_raw_public_key.restype = V
    lib.EVP_PKEY_new_raw_public_key.argtypes = [ctypes.c_int, V, P, N]
    lib.EVP_MD_CTX_new.restype = V
    lib.EVP_DigestVerifyInit.argtypes = [V, V, V, V, V]
    lib.EVP_DigestVerify.argtypes = [V, P, N, P, N]
    lib.EVP_MD_CTX_free.argtypes = [V]
    lib.EVP_PKEY_free.argtypes = [V]
    pkey = lib.EVP_PKEY_new_raw_public_key(1087, None, key, len(key))   # NID_ED25519, the same in 1.1.1 and 3.x
    ctx = lib.EVP_MD_CTX_new()
    if not pkey or not ctx:
        return None
    ok = lib.EVP_DigestVerifyInit(ctx, None, None, None, pkey) == 1 and lib.EVP_DigestVerify(ctx, sig, len(sig), msg, len(msg)) == 1
    lib.EVP_MD_CTX_free(ctx)
    lib.EVP_PKEY_free(pkey)
    return ok


def via_python():   # RFC 8032 verification in plain Python (2.7 and 3.x): OpenSSL older than 1.1.1, or none at all
    q = 2 ** 255 - 19
    order = 2 ** 252 + 27742317777372353535851937790883648493
    dd = -121665 * pow(121666, q - 2, q) % q
    sqrt_m1 = pow(2, (q - 1) // 4, q)

    def le_int(bs):
        return int(binascii.hexlify(bytearray(bs)[::-1]), 16) if len(bs) else 0

    def add(P1, P2):   # extended coordinates (X, Y, Z, T)
        A = (P1[1] - P1[0]) * (P2[1] - P2[0]) % q
        B = (P1[1] + P1[0]) * (P2[1] + P2[0]) % q
        C = 2 * P1[3] * P2[3] * dd % q
        D = 2 * P1[2] * P2[2] % q
        E, F, G, H = B - A, D - C, D + C, B + A
        return (E * F % q, G * H % q, F * G % q, E * H % q)

    def mul(s, P):
        R = (0, 1, 1, 0)
        while s > 0:
            if s & 1:
                R = add(R, P)
            P = add(P, P)
            s >>= 1
        return R

    def same(P1, P2):
        return (P1[0] * P2[2] - P2[0] * P1[2]) % q == 0 and (P1[1] * P2[2] - P2[1] * P1[2]) % q == 0

    def decode(bs):
        bs = bytearray(bs)
        if len(bs) != 32:
            return None
        sign = bs[31] >> 7
        bs[31] &= 0x7F
        y = le_int(bs)
        if y >= q:
            return None
        x2 = (y * y - 1) * pow(dd * y * y + 1, q - 2, q) % q
        if x2 == 0:
            if sign:
                return None
            return (0, y, 1, 0)
        x = pow(x2, (q + 3) // 8, q)
        if (x * x - x2) % q:
            x = x * sqrt_m1 % q
        if (x * x - x2) % q:
            return None
        if (x & 1) != sign:
            x = q - x
        return (x, y, 1, x * y % q)

    if len(sig) != 64:
        return False
    A, R = decode(key), decode(sig[:32])
    s = le_int(sig[32:])
    if A is None or R is None or s >= order:
        return False
    k = le_int(hashlib.sha512(bytes(sig[:32]) + bytes(key) + bytes(msg)).digest()) % order
    gy = 4 * pow(5, q - 2, q) % q
    G = decode(bytearray(binascii.unhexlify("%064x" % gy))[::-1])
    return same(mul(s, G), add(R, mul(k, A)))


ok = None if os.environ.get("WO_ED25519_PURE") else via_libcrypto()   # WO_ED25519_PURE (tests): the plain-Python path
if ok is None:
    ok = via_python()
sys.exit(0 if ok else 1)
PYEOF
}
if [[ -n "$CASHA" ]]; then
  "${C[@]}" -k -o "$T/ca.pem" "$BASE/agent/ca.crt" || die "could not fetch $BASE/agent/ca.crt"
  [[ "$(sha256sum "$T/ca.pem" | cut -d' ' -f1)" == "$CASHA" ]] || die "the CA served by $BASE does not match --ca-sha256: not trusted, nothing installed"
  C+=(--cacert "$T/ca.pem"); PASS+=(--ca "$T/ca.pem")
fi
"${C[@]}" -o "$T/manifest.json" "$BASE/agent/manifest.json" || die "could not fetch $BASE/agent/manifest.json (address, port, firewall?)"
if [[ -n "$RKEY" ]]; then
  k="$(jget "$T/manifest.json" release_key)"; b="$(jget "$T/manifest.json" release_body)"; s="$(jget "$T/manifest.json" release_sig)"
  [[ -n "$k" && -n "$b" && -n "$s" ]] || die "$BASE serves no signed release (is keyd running?): nothing installed"
  printf '%s' "${k#ed25519:}" | base64 -d > "$T/k.raw" 2>/dev/null || true
  [[ "$(wc -c < "$T/k.raw" | tr -d ' ')" == 32 ]] || die "the release key in the manifest is malformed"
  fp="$(key_fp "$T/k.raw")"; [[ -n "$fp" ]] || die "openssl or python3 is needed to check the release key: install one, or use the https command"
  [[ "$fp" == "$RKEY" ]] || die "release key $fp is not the one in the command ($RKEY): not installed"
  printf '%s' "$b" | base64 -d > "$T/release.json"; printf '%s' "$s" | base64 -d > "$T/release.sig"
  { printf 'wo-release-v1\n'; cat "$T/release.json"; } > "$T/release.msg"
  rc=0; ed25519_verify "$T/k.raw" "$T/release.sig" "$T/release.msg" || rc=$?
  case "$rc" in
    0) SUMS="$T/release.json" ;;
    1) die "the manifest's signature does not verify against release key $fp: not installed" ;;
    *) [[ "$BASE" == https://* ]] || die "this server cannot check an Ed25519 signature (needs OpenSSL 3, or python3 with libcrypto): use the https command of the Agents panel"
       printf 'note: the signature is not checked here (no OpenSSL 3 / python3); TLS and the pinned key fingerprint vouch for the manifest\n' >&2
       SUMS="$T/manifest.json" ;;
  esac
elif [[ "$BASE" == https://* ]]; then
  SUMS="$T/manifest.json"            # TLS (system CAs or the pinned CA) vouches for the answer
else
  die "plain http needs --release-key SHA256:… (copy the command from the Agents panel)"
fi
want="$(sed -n 's/.*"agent-install.sh": *{[^}]*"sha256": *"\([0-9a-f]\{64\}\)".*/\1/p' "$SUMS")"
[[ -n "$want" ]] || die "the manifest lists no installer"
"${C[@]}" -o "$T/install.sh" "$BASE/agent/install.sh" || die "could not fetch $BASE/agent/install.sh"
[[ "$(sha256sum "$T/install.sh" | cut -d' ' -f1)" == "$want" ]] || die "the installer does not match the signed manifest: not run"
WO_BOOT=1 bash "$T/install.sh" "${PASS[@]}"
