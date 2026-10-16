#!/bin/sh
# Watchover edge certificates (nginx): a private CA of this installation and a server certificate for the edge's names / addresses.
#   scripts/edge-cert.sh CERTDIR NAME[,NAME...]      issue (or keep) certs for these names; re-issued when the names change or
#                                                    fewer than 30 days remain; prints what it did
#   scripts/edge-cert.sh CERTDIR --check             exit 0 while the server certificate is valid for 30 more days
# Files in CERTDIR (./certs):
#   watchover-root.crt   the CA's public root (0644): colleagues' browsers trust it once; the receiver serves it as /agent/ca.crt
#                        and the Agents panel's https install command pins it by its sha256
#   .ca/watchover-ca.key the CA's private key (0600, never leaves this folder; the nightly backup carries it)
#   watchover.crt / .key the edge's certificate (leaf + CA) and key (0600)
# A company certificate (watchover.crt + watchover.key placed there by hand, no .ca/issued marker) is never touched.
# Works with OpenSSL 1.1 / 3 (RHEL, Debian, Ubuntu) and LibreSSL (macOS).
set -eu
dir="${1:?usage: edge-cert.sh CERTDIR NAME[,NAME...] | --check}"; names="${2:?usage: edge-cert.sh CERTDIR NAME[,NAME...] | --check}"
command -v openssl >/dev/null 2>&1 || { echo "edge-cert: openssl is required (RHEL: dnf install openssl)" >&2; exit 2; }
mkdir -p "$dir/.ca"; chmod 700 "$dir/.ca"
crt="$dir/watchover.crt"; key="$dir/watchover.key"; mark="$dir/.ca/issued"
valid() { [ -f "$crt" ] && openssl x509 -in "$crt" -noout -checkend $((30 * 86400)) >/dev/null 2>&1; }
if [ "$names" = "--check" ]; then valid; exit $?; fi
if [ -f "$crt" ] && [ -f "$key" ] && [ ! -f "$mark" ]; then
  echo "edge-cert: company certificate in $dir kept (watchover.crt + watchover.key)"; exit 0
fi
names=$(printf '%s' "$names" | tr ' ' ',' | tr -s ',' | sed 's/^,//; s/,$//')
[ -n "$names" ] || { echo "edge-cert: no name" >&2; exit 2; }
case "$names" in *[!A-Za-z0-9.,:_-]*) echo "edge-cert: names may hold letters, digits and . - _ : only: $names" >&2; exit 2 ;; esac
if [ -f "$mark" ] && [ "$(cat "$mark")" = "$names" ] && valid && [ -f "$dir/watchover-root.crt" ]; then
  echo "edge-cert: certificate for $names is valid (more than 30 days left)"; exit 0
fi
umask 077
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cakey="$dir/.ca/watchover-ca.key"; root="$dir/watchover-root.crt"
if [ ! -f "$cakey" ] || ! openssl x509 -in "$root" -noout -checkend $((365 * 86400)) >/dev/null 2>&1; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$cakey"
  cat > "$tmp/ca.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
O = Watchover
CN = Watchover edge CA ($(hostname 2>/dev/null | cut -d. -f1))
[v3_ca]
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
CNF
  openssl req -new -x509 -days 3650 -sha256 -key "$cakey" -config "$tmp/ca.cnf" -out "$root.tmp"
  chmod 644 "$root.tmp"; mv -f "$root.tmp" "$root"
  echo "edge-cert: new CA root $root (colleagues' browsers trust it once; agents pin it by the sha256 the Agents panel shows)"
fi
first="${names%%,*}"; san=""; i=0; j=0
for n in $(printf '%s' "$names" | tr ',' ' '); do
  if printf '%s' "$n" | grep -Eq '^[0-9]+(\.[0-9]+){3}$|:'; then j=$((j + 1)); san="$san
IP.$j = $n"; else i=$((i + 1)); san="$san
DNS.$i = $n"; fi
done
cat > "$tmp/srv.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
[dn]
O = Watchover
CN = $first
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash
[alt]$san
CNF
openssl ecparam -name prime256v1 -genkey -noout -out "$tmp/srv.key"
openssl req -new -sha256 -key "$tmp/srv.key" -config "$tmp/srv.cnf" -out "$tmp/srv.csr"
# 397 days: within what browsers accept for a server certificate; 'start' / 'upgrade' re-issue it 30 days before the end
openssl x509 -req -sha256 -days 397 -in "$tmp/srv.csr" -CA "$root" -CAkey "$cakey" -CAcreateserial -CAserial "$dir/.ca/serial" \
  -extfile "$tmp/srv.cnf" -extensions ext -out "$tmp/srv.crt" 2>/dev/null
cat "$tmp/srv.crt" "$root" > "$tmp/chain.crt"
mv -f "$tmp/srv.key" "$key"; chmod 600 "$key"
chmod 644 "$tmp/chain.crt"; mv -f "$tmp/chain.crt" "$crt"
printf '%s' "$names" > "$mark"
echo "edge-cert: certificate for $names issued ($(openssl x509 -in "$crt" -noout -enddate | sed 's/notAfter=/valid until /'))"
