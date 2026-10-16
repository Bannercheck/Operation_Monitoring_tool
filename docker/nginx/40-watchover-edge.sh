#!/bin/sh
# Runs at the nginx container's start (/docker-entrypoint.d), after the template is filled in:
#  - resolver.inc: the container network's DNS server from /etc/resolv.conf (Docker 127.0.0.11, Podman the network gateway)
#  - realip.inc: the trusted load balancers from WATCHOVER_EDGE_TRUSTED ("10.0.0.5/32 10.0.1.0/24", ./watchover.sh edge lb). Only
#    from those addresses is X-Forwarded-For believed; with none set, the client address is the connecting one and a forged
#    header changes nothing.
#  - the image's own default.conf goes (it would claim port 80 a second time).
set -eu
rm -f /etc/nginx/conf.d/default.conf
ns=$(awk '$1 == "nameserver" && $2 !~ /%/ { if ($2 ~ /:/) printf "[%s] ", $2; else printf "%s ", $2 }' /etc/resolv.conf 2>/dev/null || true)
printf 'resolver %s valid=10s ipv6=off;\nresolver_timeout 3s;\n' "${ns:-127.0.0.11}" > /etc/nginx/conf.d/resolver.inc
out=/etc/nginx/conf.d/realip.inc
: > "$out"
ok=""
for c in ${WATCHOVER_EDGE_TRUSTED:-}; do
  case "$c" in
    *[!0-9a-fA-F.:/]*|"") echo "watchover-edge: ignoring trusted proxy '$c' (not an address / CIDR)" >&2 ;;
    *) printf 'set_real_ip_from %s;\n' "$c" >> "$out"; ok="$ok $c" ;;
  esac
done
if [ -n "$ok" ]; then
  printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' >> "$out"
  echo "watchover-edge: client addresses from X-Forwarded-For of:$ok"
fi
