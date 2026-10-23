#!/bin/sh
# Runs at the nginx container's start (/docker-entrypoint.d), after the template is filled in:
#  - resolver.inc: the container network's DNS server, the first of /etc/resolv.conf (Docker 127.0.0.11, Podman the network gateway)
#  - realip.inc: the trusted load balancers from WATCHOVER_EDGE_TRUSTED ("10.0.0.5/32 10.0.1.0/24", ./watchover.sh edge lb). Only
#    from those addresses is X-Forwarded-For believed; with none set, the client address is the connecting one and a forged
#    header changes nothing.
#  - tls.inc: the TLS versions: 1.2 and 1.3 (default), or 1.3 only with WATCHOVER_EDGE_TLS_MIN=1.3 (./watchover.sh edge tls 1.3)
#  - the image's own default.conf goes (it would claim port 80 a second time).
set -eu
rm -f /etc/nginx/conf.d/default.conf
case "${WATCHOVER_EDGE_TLS_MIN:-1.2}" in
  1.3) printf 'ssl_protocols TLSv1.3;\n' > /etc/nginx/conf.d/tls.inc; echo "watchover-edge: TLS 1.3 only" ;;
  1.2|"") printf 'ssl_protocols TLSv1.2 TLSv1.3;\n' > /etc/nginx/conf.d/tls.inc ;;
  *) echo "watchover-edge: WATCHOVER_EDGE_TLS_MIN is 1.2 or 1.3, not '${WATCHOVER_EDGE_TLS_MIN}': using 1.2" >&2
     printf 'ssl_protocols TLSv1.2 TLSv1.3;\n' > /etc/nginx/conf.d/tls.inc ;;
esac
# only the FIRST nameserver: the container network's own DNS (Docker 127.0.0.11, Podman's aardvark-dns on the gateway), which
# knows the container names and forwards the rest. Podman 4 (RHEL 8) also lists the host's resolvers after it; nginx asks every
# listed server in turn and takes an NXDOMAIN from the host's resolver for "watchover" as the answer: 502 now and then.
ns=$(awk '$1 == "nameserver" && $2 !~ /%/ { if ($2 ~ /:/) print "[" $2 "]"; else print $2; exit }' /etc/resolv.conf 2>/dev/null || true)
printf 'resolver %s valid=10s ipv6=off;\nresolver_timeout 3s;\n' "${ns:-127.0.0.11}" > /etc/nginx/conf.d/resolver.inc
out=/etc/nginx/conf.d/realip.inc
: > "$out"
ok=""
for c in ${WATCHOVER_EDGE_TRUSTED:-}; do
  case "$c" in
    *[!0-9a-fA-F.:/]*|"") echo "watchover-edge: ignoring trusted proxy '$c' (not an address / CIDR)" >&2 ;;
    *) if printf '%s' "$c" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$|^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}(/[0-9]{1,3})?$'; then
         printf 'set_real_ip_from %s;\n' "$c" >> "$out"; ok="$ok $c"
       else echo "watchover-edge: ignoring trusted proxy '$c' (not an address / CIDR)" >&2; fi ;;   # "/" passed the old check: nginx refused to start
  esac
done
if [ -n "$ok" ]; then
  printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' >> "$out"
  echo "watchover-edge: client addresses from X-Forwarded-For of:$ok"
fi
