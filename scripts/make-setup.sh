#!/usr/bin/env bash
# The one-file installer (sudo watchover setup [--offline]): a shell script with the release appended, run on the target server
# like a setup program (scripts/setup-stub.sh: checks, wizard, install-rhel.sh, result).
#
#   watchover-kurulum-<ver>.sh                       online: the source tree (~40 MB); the server builds the images (internet
#                                                    or a company mirror), any CPU architecture
#   watchover-kurulum-<ver>-<arch>-hepsi-dahil.sh    --offline: also the offline bundle (images, Ollama models, training
#                                                    weights, Compose; ./watchover.sh bundle) and Podman's RPMs for this RHEL
#                                                    major version: no internet and no repository needed on the target. Built on
#                                                    an installed server with internet of the same CPU architecture (and RHEL
#                                                    major version, for the RPMs)
#
# Options: --offline  --out DIR (default ./dist)  --no-rpms (offline without the RPMs: the target has Satellite / a mirror)
# Test hooks: WATCHOVER_SETUP_BUNDLE (an existing bundle instead of ./watchover.sh bundle), WATCHOVER_SETUP_RPMS (a folder of RPMs
# with its MANIFEST instead of dnf download).
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
OFFLINE=0; OUT=dist; RPMS=1
while [ $# -gt 0 ]; do case "$1" in
  --offline) OFFLINE=1 ;; --out) OUT="${2:?--out DIR}"; shift ;; --no-rpms) RPMS=0 ;;
  *) echo "usage: watchover setup [--offline] [--out DIR] [--no-rpms]" >&2; exit 2 ;;
esac; shift; done
say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
VER=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1); [ -n "$VER" ] || die "pyproject.toml has no version"
ARCH=$(uname -m | sed 's/^arm64$/aarch64/; s/^amd64$/x86_64/')
HDR=65536                                                   # the script part is padded to this size; the archive starts after it
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
tmp=$(mktemp -d "${TMPDIR:-/var/tmp}/watchover-setup.XXXXXX"); trap 'rm -rf "$tmp"' EXIT
st="$tmp/p"; mkdir -p "$st/watchover-$VER"

say "Kaynak ağacı (v$VER)"
if git rev-parse HEAD >/dev/null 2>&1; then
  [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] || printf '  ! commit edilmemiş değişiklikler pakete girmez (git archive HEAD)\n'
  git archive --format=tar HEAD | tar xf - -C "$st/watchover-$VER"
else
  tar cf - --exclude=./.git --exclude=./.releases --exclude=./.updater --exclude=./data --exclude=./backups --exclude=./certs --exclude=./.env \
    --exclude='./*.tar' --exclude='./*.tgz' --exclude=./web/node_modules --exclude=./dist --exclude='./watchover-kurulum-*' --exclude=./.install-files . \
    | tar xf - -C "$st/watchover-$VER"
fi
[ -f "$st/watchover-$VER/install-rhel.sh" ] || die "install-rhel.sh missing from the tree"

KIND=online; COMPRESS=gzip; OSL=""; NAME="watchover-kurulum-$VER.sh"; LABEL="internetli"
if [ "$OFFLINE" = 1 ] && [ -z "${WATCHOVER_SETUP_BUNDLE:-}" ]; then   # the bundle, then the file: twice the images and models (~10-30 GB)
  for d in "$(dirname "$tmp")" "$OUT"; do
    f=$(df -Pk "$d" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1048576}')
    [ "${f:-0}" -ge 30 ] || die "$d içinde ${f:-?} GB boş: hepsi dahil paket için en az 30 GB gerekir (geçici yer: TMPDIR=KLASÖR, çıktı: --out KLASÖR)"
  done
fi
if [ "$OFFLINE" = 1 ]; then
  KIND=offline; COMPRESS=none; NAME="watchover-kurulum-$VER-$ARCH-hepsi-dahil.sh"; LABEL="hepsi dahil, internetsiz kurulur"
  if [ -n "${WATCHOVER_SETUP_BUNDLE:-}" ]; then cp "$WATCHOVER_SETUP_BUNDLE" "$st/watchover-bundle-$VER.tar"
  else say "İmajlar, modeller ve Compose (./watchover.sh bundle)"; bash ./watchover.sh bundle "$st/watchover-bundle-$VER.tar"; fi
  if [ "$RPMS" = 1 ]; then
    os_id=$( (. /etc/os-release 2>/dev/null; echo "${ID:-}") ); major=$( (. /etc/os-release 2>/dev/null; echo "${VERSION_ID%%.*}") )   # a subshell: os-release sets NAME
    if [ -n "${WATCHOVER_SETUP_RPMS:-}" ]; then cp -r "$WATCHOVER_SETUP_RPMS" "$st/rpms"
    elif command -v dnf >/dev/null 2>&1 && case "$os_id" in rhel|rocky|almalinux|ol|centos) true ;; *) false ;; esac; then
      say "Podman RPM'leri (el$major, $ARCH; bağımlılıklarıyla)"
      pk="podman container-selinux crun netavark aardvark-dns openssl chrony"; [ "$major" != 8 ] || pk="$pk podman-plugins"
      mkdir -p "$st/rpms"
      # shellcheck disable=SC2086
      if dnf -q download --resolve --alldeps --destdir "$st/rpms" $pk >/dev/null 2>&1 || dnf -q download --resolve --destdir "$st/rpms" $pk >/dev/null 2>&1; then
        printf 'os=el%s\narch=%s\npackages=%s\n' "$major" "$ARCH" "$pk" > "$st/rpms/MANIFEST"
        printf '  ✓ %s RPM (%s)\n' "$(ls "$st/rpms"/*.rpm | wc -l)" "$(du -sh "$st/rpms" | cut -f1)"
      else rm -rf "$st/rpms"; printf "  ! RPM'ler indirilemedi (dnf download): hedef sunucuda Podman ya kurulu ya da paket deposu (Satellite) olmalı\n"; fi
    else printf "  ! bu makine RHEL ailesi değil: pakete RPM eklenmedi (hedef sunucuda paket deposu gerekir)\n"; fi
  fi
  [ ! -f "$st/rpms/MANIFEST" ] || OSL="$(sed -n 's/^os=//p' "$st/rpms/MANIFEST") $ARCH"
fi

header() {   # SHA SIZE -> the script part, padded to $HDR bytes
  local f="$tmp/hdr"
  sed -e "s|@VERSION@|$VER|g" -e "s|@KIND@|$KIND|g" -e "s|@KIND_LABEL@|$LABEL|g" -e "s|@ARCH@|$([ "$KIND" = offline ] && echo "$ARCH" || echo any)|g" \
      -e "s|@OS@|$OSL|g" -e "s|@SHA256@|$1|g" -e "s|@COMPRESS@|$COMPRESS|g" -e "s|@NAME@|$NAME|g" \
      -e "s|@OFFSET@|$(printf '%-15s' "$HDR")|" -e "s|@SIZE@|$(printf '%-15s' "$2")|" scripts/setup-stub.sh > "$f"
  local n; n=$(wc -c < "$f"); [ "$n" -lt "$HDR" ] || die "setup-stub.sh is larger than $HDR bytes"
  { cat "$f"; printf '#%.0s' $(seq 1 $((HDR - n - 1))); printf '\n'; } > "$f.pad"
  [ "$(wc -c < "$f.pad")" = "$HDR" ] || die "header padding"; cat "$f.pad"
}
say "Paket yazılıyor: $OUT/$NAME"
dst="$OUT/$NAME.part"
header "$(printf '0%.0s' $(seq 64))" 0 > "$dst"
sum=$( (cd "$st" && tar cf - .) | if [ "$COMPRESS" = gzip ]; then gzip -6; else cat; fi | tee -a "$dst" | sha256sum | cut -d' ' -f1)
size=$(( $(stat -c %s "$dst") - HDR ))
header "$sum" "$size" | dd of="$dst" conv=notrunc status=none
chmod 755 "$dst"; mv "$dst" "$OUT/$NAME"
# the file's own size (du right after writing counts XFS's speculative preallocation: a 15 GB file showed as 20 GB for a while)
printf '\033[32m✓\033[0m %s  (%s, sha256 %s…)\n' "$OUT/$NAME" "$(numfmt --to=iec --suffix=B "$(stat -c %s "$OUT/$NAME")" 2>/dev/null || du -h --apparent-size "$OUT/$NAME" | cut -f1)" "${sum:0:16}"
printf '  Hedef sunucuya kopyalayın ve çalıştırın:  sudo bash %s\n' "$NAME"
[ "$KIND" = online ] || printf '  Bu paket %s içindir%s.\n' "$ARCH" "$([ -n "$OSL" ] && echo "; Podman RPM'leri ${OSL%% *} sunucular için" || echo "; Podman hedefte kurulu ya da depodan gelmeli")"
