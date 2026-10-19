#!/bin/sh
# The edge's start (docker-compose.yml: entrypoint). A kernel that reports SVE2 without SVE (Apple M4 under RHEL 9's 5.14 kernel,
# in a VM) makes OpenSSL run SVE2 code and die (SIGILL): OPENSSL_armcap then names only what the CPU really has (NEON, AES,
# SHA1/2/512, PMULL; same rule as watchover/__init__.py). Everywhere else nothing is set. Then the image's own entrypoint.
f=" $(sed -n 's/^Features[[:space:]]*:[[:space:]]*//p' "${WATCHOVER_CPUINFO:-/proc/cpuinfo}" 2>/dev/null | head -1) "
case "$f" in
  *" sve2 "*)
    case "$f" in
      *" sve "*) ;;
      *) if [ -z "${OPENSSL_armcap:-}" ]; then
           c=0
           for x in asimd:1 aes:4 sha1:8 sha2:16 pmull:32 sha512:64; do case "$f" in *" ${x%%:*} "*) c=$((c + ${x##*:})) ;; esac; done
           OPENSSL_armcap=$(printf '0x%x' "$c"); export OPENSSL_armcap
           echo "watchover-edge: the kernel reports SVE2 without SVE: OPENSSL_armcap=$OPENSSL_armcap"
         fi ;;
    esac ;;
esac
exec "${WATCHOVER_EDGE_ENTRY:-/docker-entrypoint.sh}" "$@"
