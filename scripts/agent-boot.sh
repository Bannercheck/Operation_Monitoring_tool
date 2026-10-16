#!/usr/bin/env bash
# Watchover agent bootstrap: the one-line install command from the Agents panel downloads this file, checks its sha256 (in the
# command) and runs it; it verifies the installer before anything of it runs, then hands over every argument:
#   f=$(mktemp) && curl -fsSL -o "$f" http://<dashboard>:8600/agent/boot.sh && echo "<sha256>  $f" | sha256sum -c --quiet \
#     && sudo bash "$f" --url http://<dashboard>:8600/ingest --release-key SHA256:<fingerprint> --enroll-key wk_… --env prod
#   --release-key SHA256:…   the dashboard's release key (keyd): the signed manifest must verify against it (openssl >= 1.1.1)
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
if [[ -n "$CASHA" ]]; then
  "${C[@]}" -k -o "$T/ca.pem" "$BASE/agent/ca.crt" || die "could not fetch $BASE/agent/ca.crt"
  [[ "$(sha256sum "$T/ca.pem" | cut -d' ' -f1)" == "$CASHA" ]] || die "the CA served by $BASE does not match --ca-sha256: not trusted, nothing installed"
  C+=(--cacert "$T/ca.pem"); PASS+=(--ca "$T/ca.pem")
fi
"${C[@]}" -o "$T/manifest.json" "$BASE/agent/manifest.json" || die "could not fetch $BASE/agent/manifest.json (address, port, firewall?)"
if [[ -n "$RKEY" ]]; then
  command -v openssl >/dev/null 2>&1 || die "openssl (>= 1.1.1) is needed to verify the signed manifest; install it, or use the https command"
  k="$(jget "$T/manifest.json" release_key)"; b="$(jget "$T/manifest.json" release_body)"; s="$(jget "$T/manifest.json" release_sig)"
  [[ -n "$k" && -n "$b" && -n "$s" ]] || die "$BASE serves no signed release (is keyd running?): nothing installed"
  printf '%s' "${k#ed25519:}" | base64 -d > "$T/k.raw" 2>/dev/null || true
  [[ "$(wc -c < "$T/k.raw" | tr -d ' ')" == 32 ]] || die "the release key in the manifest is malformed"
  fp="SHA256:$(openssl dgst -sha256 -binary "$T/k.raw" | base64 | tr -d '=\n')"
  [[ "$fp" == "$RKEY" ]] || die "release key $fp is not the one in the command ($RKEY): not installed"
  { printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'; cat "$T/k.raw"; } > "$T/k.der"
  printf '%s' "$b" | base64 -d > "$T/release.json"; printf '%s' "$s" | base64 -d > "$T/release.sig"
  { printf 'wo-release-v1\n'; cat "$T/release.json"; } > "$T/release.msg"
  openssl pkeyutl -verify -pubin -keyform DER -inkey "$T/k.der" -rawin -in "$T/release.msg" -sigfile "$T/release.sig" >/dev/null 2>&1 \
    || die "the manifest's signature does not verify (openssl >= 1.1.1 needed): not installed"
  SUMS="$T/release.json"
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
