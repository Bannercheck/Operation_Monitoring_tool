#!/usr/bin/env bash
# Watchover agent — install on a server (Linux systemd, macOS launchd, or plain background process).
# The dashboard serves this script and the agent itself, so the server needs nothing but curl (or wget) and network access to
# the dashboard's receiver port. On Linux amd64 / arm64 the agent is the single static Go binary wo-agent (no python3):
# `wo-agent ship` ships logs, metrics and flows; `wo-agent act` is the action agent, installed dormant next to it.
# Elsewhere (macOS, other CPUs) and with --python-agent it is agent.py (python3 >= 3.9).
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --token wo_... --logs "/var/log/syslog,/var/log/app/*.log"
#   … or let the server enrol itself with the fleet key (Agents panel › bulk install); its own token is issued and stored here:
#   curl -fsSL http://<dashboard>:8600/agent/install.sh | sudo bash -s -- --url http://<dashboard>:8600/ingest --enroll-key wk_... --env prod --site IST-DC1
# Options: --url U  --token T | --enroll-key K [--name N --site S --tags "a,b"]  --logs "a,b"  --interval 10  --env prod  --dir /opt/watchover-agent  --no-service  --no-flows  --no-test  --uninstall
#   --python-agent   install agent.py instead of wo-agent (needs python3)
# Re-running the same command upgrades in place: the token, the spool and the file positions are kept (an agent.py install is
# migrated to wo-agent: its service is stopped and replaced, its token and spool carried over, positions start where it stopped).
# Action agent (Linux + systemd, amd64 / arm64; always installed, dormant until act mode is on in the dashboard):
#   --actions-token wa_…   (the fleet-key enrolment issues it by itself)   --actions-only (add the actuator to a host that already ships logs)
#   --agent-sha256 <sha>   pin the wo-agent binary (from the release notes; without a pin a plain-HTTP install is recorded "güvensiz kurulum")
# Verified install and self-update (the Agents panel's command runs this through /agent/boot.sh, which checks this file first):
#   --release-key SHA256:…   the dashboard's release key (keyd): the signed manifest must verify against it (OpenSSL 3, or python3 / platform-python with OpenSSL 1.1.1),
#                            every file is taken from it, and the key is pinned for the agent's self-update (watchover-update)
#   --ca-sha256 <sha256>     https with the edge's own CA: /agent/ca.crt is accepted only with this sha256 and installed
#   --gate SECONDS           after (re)starting wo-agent, wait this long for it to reach the receiver; an agent.py install
#                            migrated in this run is put back when it does not (agent.py's self-migration uses it)
#   --dispatch-pub / --anchor-pub "ed25519:…"   pin the dashboard's keys (else from the enrolment answer over https, else on first use over https)
#   --token-file FILE   the agent token from a file (read once, then removed) instead of --token on the command line
#   --actions-packs linux-diag,linux-act   --actions-max-risk R0   --criticality normal|high|critical (high/critical: diagnose only)
#   --ca /etc/pki/firma-root.pem   CA (PEM) to trust for an https receiver (both agents)
set -euo pipefail
URL=""; TOKEN=""; LOGS=""; INTERVAL=10; ENV_=""; DIR=""; FLOWS=1; SERVICE=1; TEST=1; UNINSTALL=0; EKEY=""; NAME=""; SITE=""; TAGS=""
ATOKEN=""; ACT_ONLY=0; AGENT_SHA=""; DPUB=""; APUB=""; PACKS="linux-diag,linux-act"; MAXRISK="R0"; CRIT="normal"; CA=""; PYAGENT=0
RELKEY=""; CASHA=""; GATE=0
while [[ $# -gt 0 ]]; do case "$1" in
  --url) URL="$2"; shift;; --token) TOKEN="$2"; shift;;
  --token-file) TOKEN="$(head -c 512 "$2" 2>/dev/null | tr -d '\r\n')"; rm -f "$2"; shift;;   # read once, removed: not on the command line (ps)
  --logs) LOGS="$2"; shift;; --interval) INTERVAL="$2"; shift;; --env) ENV_="$2"; shift;;
  --enroll-key) EKEY="$2"; shift;; --name) NAME="$2"; shift;; --site) SITE="$2"; shift;; --tags) TAGS="$2"; shift;;
  --actions-token) ATOKEN="$2"; shift;; --actions-only) ACT_ONLY=1;; --agent-sha256) AGENT_SHA="$2"; shift;;
  --dispatch-pub) DPUB="$2"; shift;; --anchor-pub) APUB="$2"; shift;; --actions-packs) PACKS="$2"; shift;;
  --actions-max-risk) MAXRISK="$2"; shift;; --criticality) CRIT="$2"; shift;; --ca) CA="$2"; shift;; --python-agent) PYAGENT=1;;
  --release-key) RELKEY="$2"; shift;; --ca-sha256) CASHA="$2"; shift;; --gate) GATE="$2"; shift;;
  --dir) DIR="$2"; shift;; --no-service) SERVICE=0;; --no-flows) FLOWS=0;; --no-test) TEST=0;; --uninstall) UNINSTALL=1;; *) echo "unknown option: $1"; exit 1;; esac; shift; done
log(){ printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }
OS="$(uname -s)"; ROOT=0; [[ "$(id -u)" == "0" ]] && ROOT=1
# WO_INSTALL_ROOT (tests only): every system path below lives under this directory, the script acts as root without
# changing owners or users, and systemctl is whatever the PATH provides. Not for production use.
R="${WO_INSTALL_ROOT:-}"; SANDBOX=0; [[ -n "$R" ]] && { SANDBOX=1; ROOT=1; R="${R%/}"; }
if [[ -z "$DIR" ]]; then if [[ $ROOT -eq 1 ]]; then DIR="$R/opt/watchover-agent"; else DIR="$HOME/.watchover-agent"; fi; fi
UNIT=watchover-agent; UNITDIR="$R/etc/systemd/system"
if [[ $ROOT -eq 1 ]]; then PLIST="/Library/LaunchDaemons/com.watchover.agent.plist"; else PLIST="$HOME/Library/LaunchAgents/com.watchover.agent.plist"; fi
[[ "$LOGS" == *"'"* ]] && die "--logs may not contain single quotes"
HAS_SYSTEMD=0; [[ "$OS" == "Linux" && -d "$R/run/systemd/system" ]] && command -v systemctl >/dev/null 2>&1 && HAS_SYSTEMD=1
ARCH=""; case "$(uname -m)" in x86_64|amd64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; esac

# the Go binary and the action agent (wo-agent act): fixed system locations, root-owned (docs/design/action-agent.md §5.2)
AUNIT=watchover-actuator; UUNIT=watchover-update; ABIN="$R/usr/local/lib/watchover"; AETC="$R/etc/watchover"; ASTATE="$R/var/lib/watchover"; ASCRIPTS="$R/usr/lib/watchover"; AUSER=wo-act
OWN=(-o root -g root); [[ $SANDBOX -eq 1 ]] && OWN=()

if [[ $UNINSTALL -eq 1 ]]; then
  log "Removing the agent"
  if [[ $HAS_SYSTEMD -eq 1 ]]; then
    systemctl disable --now "$AUNIT" 2>/dev/null || true; rm -f "$UNITDIR/$AUNIT.service"
  fi
  if [[ $ROOT -eq 1 ]]; then
    rm -rf "$ABIN" "$AETC" "$ASTATE" "$ASCRIPTS"
    if [[ $SANDBOX -eq 0 ]] && id "$AUSER" >/dev/null 2>&1; then userdel "$AUSER" 2>/dev/null || deluser "$AUSER" 2>/dev/null || true; fi
  fi
  if [[ $HAS_SYSTEMD -eq 1 ]]; then
    systemctl disable --now "$UUNIT.timer" 2>/dev/null || true; rm -f "$UNITDIR/$UUNIT.service" "$UNITDIR/$UUNIT.timer"
    systemctl disable --now "$UNIT" 2>/dev/null || true; rm -f "$UNITDIR/$UNIT.service"; systemctl daemon-reload || true
  elif [[ "$OS" == "Darwin" ]]; then
    if [[ $ROOT -eq 1 ]]; then launchctl bootout system/com.watchover.agent 2>/dev/null || true; else launchctl unload "$PLIST" 2>/dev/null || true; fi; rm -f "$PLIST"; fi
  [[ -f "$DIR/agent.pid" ]] && kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true
  rm -rf "$DIR"; log "Done"; exit 0
fi
if [[ $ACT_ONLY -eq 1 ]]; then
  [[ -n "$URL" && -n "$ATOKEN" ]] || die "--actions-only needs --url and --actions-token wa_... (Bağlantı ayarları › Eylem ajanları)"
else
  [[ -n "$URL" && ( -n "$TOKEN" || -n "$EKEY" ) ]] || die "--url http://<dashboard>:<port>/ingest and --token wo_... (or --enroll-key wk_...) are required (both come from the Agents panel)"
fi
[[ -z "$LOGS" ]] && LOGS="auto"   # discover the applications on this server and follow their logs
[[ "$URL" == */ingest ]] || URL="${URL%/}/ingest"
BASE="${URL%/ingest}"
[[ -z "$ATOKEN" || "$ATOKEN" == wa_* ]] || die "--actions-token must start with wa_"
[[ -z "$AGENT_SHA" || "$AGENT_SHA" =~ ^[0-9a-f]{64}$ ]] || die "--agent-sha256 must be 64 lowercase hex characters"
case "$MAXRISK" in R0|R1|R2|R3|R4) ;; *) die "--actions-max-risk must be R0..R4";; esac
case "$CRIT" in low|normal|high|critical) ;; *) die "--criticality must be low, normal, high or critical";; esac
[[ -z "$CA" || -r "$CA" ]] || die "--ca $CA is not readable"
[[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--interval must be a number of seconds"
[[ -z "$RELKEY" || "$RELKEY" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "--release-key must look like SHA256:<43 base64 characters> (Agents panel)"
[[ -z "$CASHA" || "$CASHA" =~ ^[0-9a-f]{64}$ ]] || die "--ca-sha256 must be 64 lowercase hex characters"
[[ "$GATE" =~ ^[0-9]+$ ]] || die "--gate must be a number of seconds"
[[ -n "${WO_BOOT:-}" ]] && log "Installer verified by boot.sh (signed manifest)"

fetch() {  # fetch URL FILE: curl, wget or python, nothing else (the --ca file is trusted for https)
  if command -v curl >/dev/null 2>&1; then curl -fsSL --max-time 120 ${CA:+--cacert "$CA"} -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -T 120 ${CA:+--ca-certificate="$CA"} -O "$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then python3 -c 'import ssl,sys,urllib.request; c=ssl.create_default_context(cafile=sys.argv[3] or None); open(sys.argv[2],"wb").write(urllib.request.urlopen(sys.argv[1],timeout=120,context=c).read())' "$1" "$2" "$CA"
  else return 1; fi
}

ATMP="$(mktemp -d)"; trap 'rm -rf "${ATMP:-}"' EXIT
# --ca-sha256: the edge's CA is fetched without verification and accepted only with the pinned sha256 (then trusted for https)
if [[ -n "$CASHA" ]]; then
  if command -v curl >/dev/null 2>&1; then curl -fsSLk --max-time 60 -o "$ATMP/ca.pem" "$BASE/agent/ca.crt" || die "could not fetch $BASE/agent/ca.crt"
  else wget -q -T 60 --no-check-certificate -O "$ATMP/ca.pem" "$BASE/agent/ca.crt" || die "could not fetch $BASE/agent/ca.crt"; fi
  [[ "$(sha256sum "$ATMP/ca.pem" | cut -d' ' -f1)" == "$CASHA" ]] || die "the CA served by $BASE does not match --ca-sha256: not trusted, nothing installed"
  CA="$ATMP/ca.pem"
fi
# a CA (--ca, or the pinned one above) is installed next to the agent, so both agents keep trusting it after this script is gone
if [[ -n "$CA" ]]; then
  if [[ $ROOT -eq 1 ]]; then CADIR="$AETC"; COWN=(${OWN[@]+"${OWN[@]}"}); else CADIR="$DIR"; COWN=(); fi
  mkdir -p "$CADIR"
  if [[ "$CA" != "$CADIR/ca.pem" ]]; then install ${COWN[@]+"${COWN[@]}"} -m 0644 "$CA" "$CADIR/ca.pem"; fi
  CA="$CADIR/ca.pem"
fi

jget() { sed -n "s/.*\"$2\": *\"\([^\"]*\)\".*/\1/p" "$1" | head -n1; }
sumof() { sed -n "s/.*\"$2\": *{[^}]*\"sha256\": *\"\([0-9a-f]\{64\}\)\".*/\1/p" "$1" | head -n1; }   # sumof JSON NAME
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
# The signed release (keyd): /agent/manifest.json carries release_body / release_sig / release_key. With --release-key the key
# must have that fingerprint and the signature must verify (OpenSSL 3, or libcrypto through python), else nothing is installed; without it the key
# is taken as served (pinned on first use: over plain http that is no proof, and the host stays "güvensiz kurulum").
# Sets RKEY_PIN (ed25519:…) and SUMS (the file whose sha256 values are trusted).
MANIFEST=""; RKEY_PIN=""; SUMS=""; SIGNED=0
load_manifest() {
  [[ -n "$MANIFEST" ]] && return 0
  fetch "$BASE/agent/manifest.json" "$ATMP/manifest.json" || return 1
  MANIFEST="$ATMP/manifest.json"; SUMS="$MANIFEST"
  local k b s fp
  k="$(jget "$MANIFEST" release_key)"; b="$(jget "$MANIFEST" release_body)"; s="$(jget "$MANIFEST" release_sig)"
  if [[ -z "$k" || -z "$b" || -z "$s" ]]; then
    [[ -z "$RELKEY" ]] || die "--release-key given but $BASE serves no signed release (is keyd running?): nothing installed"
    return 0
  fi
  printf '%s' "${k#ed25519:}" | base64 -d > "$ATMP/rk.raw" 2>/dev/null || true
  [[ "$(wc -c < "$ATMP/rk.raw" | tr -d ' ')" == 32 ]] || die "the release key in the manifest is malformed"
  fp="$(key_fp "$ATMP/rk.raw")"
  if [[ -z "$fp" ]]; then
    [[ -z "$RELKEY" ]] || die "openssl or python3 is needed to check the release key: install one, or use the https command"
    log "neither openssl nor python3: the signed manifest is not checked here (the agent checks it on every self-update)"
    RKEY_PIN="$k"; return 0
  fi
  [[ -z "$RELKEY" || "$fp" == "$RELKEY" ]] || die "release key $fp is not the one in the command ($RELKEY): not installed"
  printf '%s' "$b" | base64 -d > "$ATMP/release.json"; printf '%s' "$s" | base64 -d > "$ATMP/release.sig"
  { printf 'wo-release-v1\n'; cat "$ATMP/release.json"; } > "$ATMP/release.msg"
  local rc=0; ed25519_verify "$ATMP/rk.raw" "$ATMP/release.sig" "$ATMP/release.msg" || rc=$?
  if [[ "$rc" == 1 ]]; then die "the manifest's signature does not verify against release key $fp: not installed"; fi
  if [[ "$rc" != 0 ]]; then
    [[ -z "$RELKEY" || "$BASE" == https://* ]] || die "this server cannot check an Ed25519 signature (needs OpenSSL 3, or python3 with libcrypto): use the https command"
    log "the signature is not checked here (no OpenSSL 3 / python3): $([[ "$BASE" == https://* ]] && echo "TLS and the key fingerprint vouch for the manifest" || echo "taken as served"); the agent checks every self-update"
    RKEY_PIN="$k"; return 0
  fi
  RKEY_PIN="$k"; SUMS="$ATMP/release.json"; SIGNED=1
  log "Signed release $(jget "$MANIFEST" release_version) verified (release key $fp)"
}

# wo-agent from the dashboard's receiver, sha256-checked against the signed release (else /agent/manifest.json), and against
# --agent-sha256 when given. Returns 1 (WOBIN stays empty) when this receiver does not serve a build for this host; a
# checksum mismatch stops the install.
WOBIN=""; INSECURE=0
fetch_wo_agent() {
  [[ -n "$WOBIN" ]] && return 0
  [[ "$OS" == "Linux" && -n "$ARCH" ]] || return 1
  load_manifest || return 1
  fetch "$BASE/agent/wo-agent-linux-$ARCH" "$ATMP/wo-agent" || return 1
  local want got; got="$(sha256sum "$ATMP/wo-agent" | cut -d' ' -f1)"
  want="$(sumof "$SUMS" "wo-agent-linux-$ARCH")"
  [[ -n "$want" && "$got" == "$want" ]] || die "wo-agent checksum does not match the $([[ $SIGNED -eq 1 ]] && echo signed )manifest (got $got): download corrupted or tampered with"
  if [[ -n "$AGENT_SHA" ]]; then
    [[ "$got" == "$AGENT_SHA" ]] || die "wo-agent checksum $got differs from --agent-sha256 $AGENT_SHA: not installed"
  elif [[ "$BASE" == http://* && -z "$RELKEY" ]]; then
    INSECURE=1                      # neither a pinned checksum nor a pinned release key: only the same server vouches for it
  fi
  chmod 0755 "$ATMP/wo-agent"; WOBIN="$ATMP/wo-agent"
}

# The action agent: the same wo-agent binary, its allowlist and local policy written root-owned from the packs inside the
# binary, a systemd unit. It stays dormant (hello every 5 minutes, nothing else) until act mode is turned on and a person
# confirms its key.
install_actuator() {
  if [[ "$OS" != "Linux" || $ROOT -ne 1 || $HAS_SYSTEMD -ne 1 || $SERVICE -ne 1 ]]; then
    log "Action agent skipped: it needs Linux with systemd and root (this server: $OS, root=$ROOT, systemd=$HAS_SYSTEMD)"; return 0
  fi
  [[ -n "$ARCH" ]] || { log "Action agent skipped: no wo-agent build for $(uname -m) (linux amd64 / arm64 only)"; return 0; }
  fetch_wo_agent || { log "Action agent skipped: $BASE does not serve wo-agent-linux-$ARCH (receiver image without the Go agent)"; return 0; }
  if [[ $INSECURE -eq 1 ]]; then
    log "Warning: wo-agent came over plain HTTP and only the same server vouches for its checksum: recorded as 'güvensiz kurulum'"
    log "         (the dashboard refuses changes on this host until it is reinstalled with the Agents panel's command: https, or --release-key)"
  fi
  mkdir -p "$ABIN" && install ${OWN[@]+"${OWN[@]}"} -m 0755 "$WOBIN" "$ABIN/wo-agent"
  if [[ $SANDBOX -eq 0 ]] && ! id "$AUSER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$AUSER" 2>/dev/null || adduser -S -H -s /sbin/nologin "$AUSER" 2>/dev/null || true
  fi
  local sim=(); [[ $SANDBOX -eq 1 ]] && sim=(--sim)
  umask 077; printf '%s\n' "$ATOKEN" > "$ATMP/token"; umask 022
  "$ABIN/wo-agent" act install ${sim[@]+"${sim[@]}"} --actions-dir "$AETC/actions.d" --state-dir "$ASTATE/actions" --scripts-dir "$ASCRIPTS/actions" \
    --packs "$PACKS" --max-risk "$MAXRISK" --criticality "$CRIT" ${DPUB:+--dispatch-pub "$DPUB"} ${APUB:+--anchor-pub "$APUB"} \
    ${ATOKEN:+--token-file "$ATMP/token"} || die "wo-agent act install failed"
  chmod 0755 "$AETC"
  umask 077
  cat > "$AETC/wo-agent.env" <<AEOF
WO_URL='$BASE'
WO_HOST='${NAME:-$(hostname)}'
WO_INSECURE_INSTALL=$INSECURE
WO_CA='$CA'
AEOF
  umask 022; chmod 0600 "$AETC/wo-agent.env"
  mkdir -p "$UNITDIR"
  cat > "$UNITDIR/$AUNIT.service" <<UNITEOF
[Unit]
Description=Watchover action agent (wo-agent act: dormant until act mode is on and its key is confirmed)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$AETC/wo-agent.env
ExecStart=$ABIN/wo-agent act
Restart=always
RestartSec=10
Nice=10
CPUQuota=25%
MemoryMax=128M
TasksMax=64
UMask=0027
ProtectSystem=full
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  if [[ -s "$ASTATE/actions/actuator.token" ]]; then
    systemctl enable "$AUNIT" >/dev/null 2>&1 && systemctl restart "$AUNIT" || die "systemd could not start $AUNIT (journalctl -u $AUNIT)"
    log "Action agent installed (dormant). Key: $("$ABIN/wo-agent" act fingerprint --state-dir "$ASTATE/actions") — compare it in Bağlantı ayarları › Eylem ajanları"
  else
    log "Action agent installed but not started: no actuator token (add one in Bağlantı ayarları › Eylem ajanları, then re-run with --actions-only --actions-token wa_...)"
  fi
}

if [[ $ACT_ONLY -eq 1 ]]; then
  install_actuator
  log "Done. Remove with: --uninstall"
  exit 0
fi

# the collector: wo-agent on Linux amd64 / arm64 when the receiver serves it, agent.py otherwise (or with --python-agent)
ENGINE=python
if [[ $PYAGENT -eq 0 && "$OS" == "Linux" ]]; then
  if [[ -z "$ARCH" ]]; then log "No wo-agent build for $(uname -m): installing agent.py (python3)"
  elif fetch_wo_agent; then ENGINE=go
  else log "$BASE does not serve wo-agent-linux-$ARCH (receiver image without the Go agent): installing agent.py (python3)"; fi
fi

envval() {  # envval FILE KEY: a value from an agent.env this script wrote (read, never sourced)
  [[ -f "$1" ]] || return 0
  sed -n "s/^$2='\(.*\)'\$/\1/p; s/^$2=\([^']*\)\$/\1/p" "$1" | head -n1
}
OLD_URL="$(envval "$DIR/agent.env" WATCHOVER_URL)"; OLD_TOKEN="$(envval "$DIR/agent.env" WATCHOVER_TOKEN)"
OLD_PY=0
if [[ -f "$DIR/agent.py" ]] || { [[ -f "$UNITDIR/$UNIT.service" ]] && grep -q 'agent\.py' "$UNITDIR/$UNIT.service"; }; then OLD_PY=1; fi

if [[ $ENGINE == go ]]; then
  BIN="$DIR/wo-agent"; [[ $ROOT -eq 1 ]] && BIN="$ABIN/wo-agent"
  log "wo-agent → $BIN (logs, metrics, flows; no python3 needed)"
  mkdir -p "$DIR" "$(dirname "$BIN")"
  if [[ $ROOT -eq 1 ]]; then install ${OWN[@]+"${OWN[@]}"} -m 0755 "$WOBIN" "$BIN"; else install -m 0755 "$WOBIN" "$BIN"; fi
else
  log "Python"
  PY="$(command -v python3 || true)"
  if [[ -z "$PY" ]] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    if [[ $ROOT -eq 1 && $SANDBOX -eq 0 && "$OS" == "Linux" ]]; then
      if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 >/dev/null; elif command -v dnf >/dev/null; then dnf install -y -q python3; elif command -v yum >/dev/null; then yum install -y -q python3; fi
      PY="$(command -v python3 || true)"
    fi
    [[ -n "$PY" ]] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "python3 >= 3.9 is required on this server"
  fi
  log "Agent files → $DIR"
  mkdir -p "$DIR"
  WO_CA="$CA" "$PY" - "$BASE/agent.py" "$DIR/agent.py.new" <<'PYEOF' || die "could not download agent.py from $BASE/agent.py — is the dashboard reachable from this server (address, port, firewall)?"
import os, ssl, sys, urllib.request
src, dst = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(cafile=os.environ.get("WO_CA") or None)
with urllib.request.urlopen(src, timeout=15, context=ctx) as r:
    data = r.read()
assert b"Watchover agent" in data, "unexpected content"
open(dst, "wb").write(data)
PYEOF
  if [[ "$OS" == "Linux" ]] && load_manifest && [[ $SIGNED -eq 1 ]]; then
    [[ "$(sha256sum "$DIR/agent.py.new" | cut -d' ' -f1)" == "$(sumof "$SUMS" agent.py)" ]] || { rm -f "$DIR/agent.py.new"; die "agent.py does not match the signed manifest: not installed"; }
  fi
  mv -f "$DIR/agent.py.new" "$DIR/agent.py"
fi
# the release key the agent's self-update trusts (and agent.py's migration to wo-agent): pinned here, never changed by an update
if [[ "$OS" == "Linux" ]]; then load_manifest || true; fi

# the token: --token, else the one this host already has for the same dashboard (a re-run keeps its identity), else enrol
if [[ -z "$TOKEN" && -n "$OLD_TOKEN" && "$OLD_URL" == "$URL" ]]; then
  TOKEN="$OLD_TOKEN"; log "Keeping this host's agent token (same dashboard, same identity)"
fi
if [[ -z "$TOKEN" ]]; then
  log "Self-enrolment as ${NAME:-$(hostname)}"
  if [[ $ENGINE == go ]]; then
    ENROLLED="$("$BIN" enroll --url "$BASE" --enroll-key "$EKEY" --name "${NAME:-$(hostname)}" --env "$ENV_" --site "$SITE" --tags "$TAGS" ${CA:+--ca "$CA"})" \
      || die "self-enrolment failed: is the enrolment key current (Agents panel › bulk install) and the name not already enrolled?"
  else
    ENROLLED="$(WO_CA="$CA" "$PY" - "$BASE/enroll" "$EKEY" "${NAME:-$(hostname)}" "$ENV_" "$SITE" "$TAGS" <<'PYEOF'
import json, os, ssl, sys, urllib.request, urllib.error
url, key, name, env, site, tags = sys.argv[1:7]
ctx = ssl.create_default_context(cafile=os.environ.get("WO_CA") or None)
req = urllib.request.Request(url, data=json.dumps({"name": name, "env": env, "site": site, "tags": tags}).encode(),
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Agent": name}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        doc = json.loads(r.read())
        # the collector's token, then the action agent's token and the dashboard's public keys (empty when not issued yet)
        for k in ("token", "actuator_token", "dispatch_pub", "anchor_pub"):
            print(str(doc.get(k) or "").strip())
except urllib.error.HTTPError as e:
    sys.stderr.write("enrolment refused (HTTP %d): %s\n" % (e.code, (e.read() or b"").decode()[:200])); sys.exit(1)
PYEOF
)" || die "self-enrolment failed: is the enrolment key current (Agents panel › bulk install) and the name not already enrolled?"
  fi
  { read -r TOKEN; read -r _at; read -r _dp; read -r _ap; } <<<"$ENROLLED" || true
  [[ -z "$ATOKEN" ]] && ATOKEN="${_at:-}"
  if [[ "$BASE" == https://* ]]; then             # keys from the enrolment answer only over TLS (security review): over plain http
    [[ -z "$DPUB" ]] && DPUB="${_dp:-}"            # anyone on the path could swap in their own dispatch key and run actions here
    [[ -z "$APUB" ]] && APUB="${_ap:-}"
  elif [[ -n "${_dp:-}" && -z "$DPUB" ]]; then
    log "plain http: the dashboard's action keys are not pinned from the answer; pass --dispatch-pub / --anchor-pub (Otomasyon › Anahtarlar) or install over https"
  fi
  [[ -n "$TOKEN" ]] || die "self-enrolment answered without a token"
fi
# self-update (wo-agent update, a oneshot unit on a timer): Linux + systemd + root installs of wo-agent with a pinned release key
SELFUPD=0; UPD_DIR="$ASTATE/update"
[[ $ENGINE == go && $ROOT -eq 1 && $HAS_SYSTEMD -eq 1 && $SERVICE -eq 1 && -n "$RKEY_PIN" ]] && SELFUPD=1
OLD_NAME="$(envval "$DIR/agent.env" WATCHOVER_AGENT_NAME)"
ANAME="${NAME:-${OLD_NAME:-$(hostname)}}"
umask 077
cat > "$DIR/agent.env.new" <<ENVEOF
WATCHOVER_URL='$URL'
WATCHOVER_TOKEN='$TOKEN'
WATCHOVER_LOGS='$LOGS'
WATCHOVER_METRICS=1
WATCHOVER_FLOWS=$FLOWS
WATCHOVER_INTERVAL='$INTERVAL'
WATCHOVER_SPOOL='$DIR/spool'
WATCHOVER_STATE='$DIR/positions.json'
AGENT_ENV='$ENV_'
WATCHOVER_CA='$CA'
WATCHOVER_RELEASE_KEY='$RKEY_PIN'
WATCHOVER_AGENT_NAME='$ANAME'
$([[ $SELFUPD -eq 1 ]] && echo "WO_UPDATE_DIR='$UPD_DIR'" || true)
ENVEOF
chmod 600 "$DIR/agent.env.new"; mkdir -p -m 700 "$DIR/spool"; chmod 700 "$DIR/spool"
umask 022
IFS=',' read -ra _paths <<<"$LOGS"
for _p in "${_paths[@]}"; do _p="$(echo "$_p" | xargs)"; [[ -z "$_p" || "$_p" == "auto" || "$_p" == "journal" || "$_p" == "windows-events" || "$_p" == *"*"* || -r "$_p" ]] || echo "  warning: $_p does not exist yet on this server (the agent keeps waiting for it)"; done

if [[ $TEST -eq 1 ]]; then   # before anything running is touched: a failed test leaves the previous install as it was
  log "Connectivity test"
  if [[ $ENGINE == go ]]; then T=("$BIN" ship --test); else T=("$PY" "$DIR/agent.py" --test); fi
  # A token issued a moment ago (enrolment, or the dashboard just started) reaches the receiver within seconds: its 401 / 503 /
  # 502 is retried for up to ${WO_TEST_TRIES:-10} x 3 s; anything else (wrong address, port, firewall) fails at once.
  TOUT=""; _ok=0
  for _try in $(seq 1 "${WO_TEST_TRIES:-10}"); do
    if TOUT="$( set -a; . "$DIR/agent.env.new"; set +a; "${T[@]}" 2>&1 )"; then _ok=1; break; fi
    case "$TOUT" in *401*|*503*|*502*) [[ $_try -lt ${WO_TEST_TRIES:-10} ]] && { log "receiver not ready for this token yet: retrying ($_try)"; sleep "${WO_TEST_WAIT:-3}"; } ;; *) break ;; esac
  done
  [[ $_ok -eq 1 ]] || { echo "$TOUT"; rm -f "$DIR/agent.env.new"; die "the receiver at $URL did not accept this agent: check the address/port (firewall on the dashboard machine) and that the token is active"; }
  echo "$TOUT"
  # the name the dashboard knows this host by (its token decides): pins ("bu sürüme döndür") are bound to it
  _n="$(sed -n 's/.*· agent=\([^ ]*\).*/\1/p' <<<"$TOUT" | head -n1)"
  if [[ -n "$_n" && "$_n" != "None" && "$_n" != "$ANAME" && "$_n" != *"'"* ]]; then sed -i.bak "s/^WATCHOVER_AGENT_NAME=.*/WATCHOVER_AGENT_NAME='$_n'/" "$DIR/agent.env.new"; rm -f "$DIR/agent.env.new.bak"; fi
fi
mv -f "$DIR/agent.env.new" "$DIR/agent.env"

# stop what ran before (agent.py or an older wo-agent, as a service or a background process); the spool stays where it is
if [[ $HAS_SYSTEMD -eq 1 && -f "$UNITDIR/$UNIT.service" ]]; then
  if [[ $GATE -gt 0 && $OLD_PY -eq 1 && $ENGINE == go ]]; then mkdir -p -m 700 "$DIR/.migrate"; cp -p "$UNITDIR/$UNIT.service" "$DIR/.migrate/"; fi
  systemctl stop "$UNIT" 2>/dev/null || true
fi
[[ -f "$DIR/agent.pid" ]] && { kill "$(cat "$DIR/agent.pid")" 2>/dev/null || true; rm -f "$DIR/agent.pid"; }   # replace a previous background run
if [[ $ENGINE == go && ! -f "$DIR/positions.json" ]]; then
  # agent.py keeps no positions: every followed file is recorded at its end right now, the moment the old agent stopped, so
  # the new service ships exactly what is written from here on (nothing twice, no gap); a fresh install starts here as well
  [[ $OLD_PY -eq 1 ]] && log "Migrating from agent.py: token and spool kept, file positions taken over at the hand-over"
  ( set -a; . "$DIR/agent.env"; set +a; "$BIN" ship --seed-positions ) || log "Warning: positions not seeded (the agent starts at the end of each file)"
fi

if [[ $ENGINE == go ]]; then
  EXEC=("$BIN" ship); DESC="Watchover agent (wo-agent ship: logs, metrics, flows → dashboard)"
  HARDEN=$'TimeoutStopSec=30\nNoNewPrivileges=yes\nProtectSystem=full\nProtectHome=read-only\nMemoryMax=256M'
else
  EXEC=("$PY" "$DIR/agent.py"); DESC="Watchover agent (metrics + logs → dashboard)"; HARDEN=""
fi
EXECQ="$(printf '"%s" ' "${EXEC[@]}")"; EXECQ="${EXECQ% }"   # quoted for the unit file and the launchd shell line
if [[ $SERVICE -eq 1 && $ROOT -eq 1 && $HAS_SYSTEMD -eq 1 ]]; then
  log "systemd service $UNIT"
  mkdir -p "$UNITDIR"
  cat > "$UNITDIR/$UNIT.service" <<UNITEOF
[Unit]
Description=$DESC
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$DIR/agent.env
ExecStart=$EXECQ
Restart=always
RestartSec=5
$HARDEN
[Install]
WantedBy=multi-user.target
UNITEOF
  if [[ $SELFUPD -eq 1 ]]; then mkdir -p -m 700 "$UPD_DIR"; chmod 700 "$UPD_DIR"; rm -f "$UPD_DIR/hello.json"; fi
  systemctl daemon-reload && systemctl enable "$UNIT" >/dev/null 2>&1 && systemctl restart "$UNIT" || die "systemd could not start $UNIT (journalctl -u $UNIT)"
  sleep 1; systemctl --no-pager --lines=3 status "$UNIT" || true
  if [[ $GATE -gt 0 && $SELFUPD -eq 1 ]]; then   # the new agent must reach the receiver (hello.json) or the migration is undone
    WOVER="$("$BIN" version | awk '{print $2}')"; _end=$(( $(date +%s) + GATE )); _ok=0
    while (( $(date +%s) < _end )); do
      if grep -qs "\"version\":\"$WOVER\"" "$UPD_DIR/hello.json"; then _ok=1; break; fi; sleep 1
    done
    if [[ $_ok -eq 0 ]]; then
      if [[ $OLD_PY -eq 1 && -f "$DIR/.migrate/$UNIT.service" && -f "$DIR/agent.py" ]]; then
        cp -p "$DIR/.migrate/$UNIT.service" "$UNITDIR/$UNIT.service"; systemctl daemon-reload || true; systemctl restart "$UNIT" || true
        die "wo-agent $WOVER did not reach the receiver within ${GATE}s: agent.py runs again (journalctl -u $UNIT)"
      fi
      die "wo-agent $WOVER did not reach the receiver within ${GATE}s (journalctl -u $UNIT)"
    fi
    log "wo-agent $WOVER reached the receiver"
    if [[ $OLD_PY -eq 1 ]]; then   # agent.py's self-migration is done: reported by wo-agent as X-Agent-Update
      printf '{"phase":"updated","to":"%s","from":"agent.py","at":"%s"}\n' "$WOVER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$UPD_DIR/state.json"
    fi
  fi
  rm -rf "$DIR/.migrate"
  if [[ $SELFUPD -eq 1 ]]; then
    log "Self-update: $UUNIT.timer (signed releases only, verified against release key $(printf '%s' "${RKEY_PIN#ed25519:}" | cut -c1-12)…)"
    cat > "$UNITDIR/$UUNIT.service" <<UNITEOF
[Unit]
Description=Watchover agent self-update (wo-agent update: signed release, verified, rolled back when it does not come up)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
EnvironmentFile=$DIR/agent.env
ExecStart="$BIN" update
TimeoutStartSec=15min
Nice=10
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$(dirname "$BIN") $UPD_DIR
UNITEOF
    cat > "$UNITDIR/$UUNIT.timer" <<UNITEOF
[Unit]
Description=Watchover agent self-update check (every 6 hours; the receiver can also start it when it has a newer release)
[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
    systemctl daemon-reload && systemctl enable --now "$UUNIT.timer" >/dev/null 2>&1 || log "Warning: $UUNIT.timer not enabled (systemctl enable --now $UUNIT.timer)"
  fi
elif [[ $SERVICE -eq 1 && "$OS" == "Darwin" ]]; then
  log "launchd agent com.watchover.agent"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.watchover.agent</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string><string>set -a; . "$DIR/agent.env"; set +a; exec $EXECQ</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agent.log</string><key>StandardErrorPath</key><string>$DIR/agent.log</string>
</dict></plist>
PLISTEOF
  if [[ $ROOT -eq 1 ]]; then
    chown root:wheel "$PLIST"; chmod 644 "$PLIST"
    launchctl bootout system/com.watchover.agent 2>/dev/null || true; launchctl bootstrap system "$PLIST"   # LaunchDaemon: runs at boot, as root
  else
    launchctl unload "$PLIST" 2>/dev/null || true; launchctl load "$PLIST"
  fi
elif [[ $SERVICE -eq 1 ]]; then
  log "Background process (no systemd/launchd): $DIR/agent.log"
  ( set -a; . "$DIR/agent.env"; set +a; nohup "${EXEC[@]}" >>"$DIR/agent.log" 2>&1 & echo $! > "$DIR/agent.pid" )
fi
if [[ $ENGINE == go ]]; then rm -f "$DIR/agent.py"; fi   # migrated: wo-agent runs now (re-run with --python-agent to go back)
install_actuator
log "Installed ($([[ $ENGINE == go ]] && echo "wo-agent" || echo "agent.py")). Files: $DIR · settings: $DIR/agent.env · re-run with --uninstall to remove"
