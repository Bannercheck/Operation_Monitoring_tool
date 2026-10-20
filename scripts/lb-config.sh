#!/bin/sh
# Watchover: load balancer configuration for one public name (./watchover.sh edge lbconf [f5|netscaler|haproxy|all] [VIP]).
#   scripts/lb-config.sh VENDOR NAME SERVER_IP [HTTPS_PORT] [HTTP_PORT] [VIP]
# Prints ready-to-review snippets with this server's values. Two ways, both end on the server's https port:
#   re-encrypt (recommended): the LB terminates TLS with the company certificate, inserts X-Forwarded-For and opens TLS again to
#              the server (Watchover's own CA, or the same company certificate): real client addresses in logs and sign-in records
#   passthrough: plain TCP; the certificate the users see is Watchover's (edge company); client addresses are the LB's (SNAT)
# TLS: 1.2 and 1.3 only, forward-secret AEAD suites (no SSLv3 / TLS 1.0 / 1.1, CBC, RSA key exchange); the server's own edge does
# the same, or TLS 1.3 only after ./watchover.sh edge tls 1.3 (then the LB's server side must speak 1.3 as well).
# Health check: GET /healthz -> 200 with {"ok": true ...} (https on HTTPS_PORT, or http on HTTP_PORT). Idle timeout >= 300 s
# (live log tail, long reports), request bodies up to 512 MB (log uploads), no session persistence needed.
set -eu
vendor="${1:?usage: lb-config.sh f5|netscaler|haproxy|all NAME SERVER_IP [HTTPS_PORT] [HTTP_PORT] [VIP]}"
name="${2:?name}"; ip="${3:?server ip}"; hp="${4:-443}"; pp="${5:-80}"; vip="${6:-<VIP>}"
lhp="${WATCHOVER_LB_HTTPS_PORT:-443}"; lpp="${WATCHOVER_LB_HTTP_PORT:-80}"     # the LB's own listening ports (edge lbtest: 9443 / 9080)
case "$name$ip$hp$pp$vip$lhp$lpp" in *[!A-Za-z0-9.:\<\>_*-]*) echo "lb-config: unexpected characters" >&2; exit 2 ;; esac
id=$(printf '%s' "$name" | cut -d. -f1)

f5() {
cat <<EOF
# ---------------------------------------------------------------- F5 BIG-IP (tmsh) ---- $name -> $ip:$hp
# A. re-encrypt (SSL bridging) with X-Forwarded-For (recommended). Import first: certificate + key + company CA chain for $name
create ltm monitor https ${id}_healthz { defaults-from https send "GET /healthz HTTP/1.1\\r\\nHost: $name\\r\\nConnection: close\\r\\n\\r\\n" recv "\\"ok\\": *true" interval 10 timeout 31 }
create ltm pool ${id}_pool { members add { $ip:$hp { address $ip } } monitor ${id}_healthz load-balancing-mode round-robin }
# TLS 1.2 + 1.3 with AEAD suites (BIG-IP 14.1+; TLS 1.3 towards the server needs 15.1+: on older versions keep the server's
# ./watchover.sh edge tls at 1.2). The network team checks the names against its BIG-IP version (tmsh list ltm cipher rule).
create ltm cipher rule ${id}_tls { cipher "TLS13-AES128-GCM-SHA256:TLS13-AES256-GCM-SHA384:TLS13-CHACHA20-POLY1305-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384" dh-groups DEFAULT signature-algorithms DEFAULT }
create ltm cipher group ${id}_tls { allow replace-all-with { ${id}_tls { } } }
create ltm profile client-ssl ${id}_clientssl { defaults-from clientssl cert-key-chain replace-all-with { $id { cert $name.crt key $name.key chain company-ca-chain.crt } } ciphers none cipher-group ${id}_tls options { dont-insert-empty-fragments no-ssl no-tlsv1 no-tlsv1.1 } renegotiation disabled }
create ltm profile server-ssl ${id}_serverssl { defaults-from serverssl server-name $name ciphers none cipher-group ${id}_tls options { dont-insert-empty-fragments no-ssl no-tlsv1 no-tlsv1.1 } }
create ltm profile http ${id}_http { defaults-from http insert-xforwarded-for enabled }
create ltm profile tcp ${id}_tcp { defaults-from tcp idle-timeout 300 }
create ltm virtual ${id}_vs_443 { destination $vip:$lhp ip-protocol tcp pool ${id}_pool source-address-translation { type automap } profiles replace-all-with { ${id}_tcp { } ${id}_http { } websocket { } ${id}_clientssl { context clientside } ${id}_serverssl { context serverside } } }
create ltm virtual ${id}_vs_80 { destination $vip:$lpp ip-protocol tcp profiles replace-all-with { tcp { } http { } } rules { _sys_https_redirect } }
# then on the Watchover server: ./watchover.sh edge lb <the BIG-IP self / SNAT addresses>   (X-Forwarded-For believed only from them)
#
# B. passthrough (no TLS on the BIG-IP; users see Watchover's certificate: ./watchover.sh edge csr / edge company)
# create ltm virtual ${id}_vs_443 { destination $vip:$lhp ip-protocol tcp pool ${id}_pool source-address-translation { type automap } profiles replace-all-with { fastL4 { } } }

EOF
}

netscaler() {
cat <<EOF
# ---------------------------------------------------------------- Citrix NetScaler ADC ---- $name -> $ip:$hp
# re-encrypt (SSL offload + SSL to the server) with X-Forwarded-For. Upload first: $name.crt / $name.key and the company CA chain
add server ${id}-srv $ip
add lb monitor ${id}-healthz HTTP-ECV -send "GET /healthz" -recv "\"ok\":true" -secure YES -customHeaders "Host: $name\r\n"
add serviceGroup ${id}-sg SSL -cip ENABLED X-Forwarded-For -cltTimeout 300 -svrTimeout 300
bind serviceGroup ${id}-sg ${id}-srv $hp
bind serviceGroup ${id}-sg -monitorName ${id}-healthz
add ssl certKey ${id}-cert -cert $name.crt -key $name.key
add lb vserver ${id}-vs SSL $vip $lhp -persistenceType NONE -cltTimeout 300
bind lb vserver ${id}-vs ${id}-sg
bind ssl vserver ${id}-vs -certkeyName ${id}-cert
set ssl serviceGroup ${id}-sg -sniEnable ENABLED -commonName $name -ssl3 DISABLED -tls1 DISABLED -tls11 DISABLED -tls12 ENABLED -tls13 ENABLED
# TLS 1.2 + 1.3 only, AEAD suites (ADC 13.0+ for TLS 1.3; the network team checks against its version: show ssl cipher)
set ssl vserver ${id}-vs -ssl3 DISABLED -tls1 DISABLED -tls11 DISABLED -tls12 ENABLED -tls13 ENABLED -denySSLReneg ALL
unbind ssl vserver ${id}-vs -cipherName DEFAULT
bind ssl vserver ${id}-vs -cipherName TLS1.3-AES128-GCM-SHA256
bind ssl vserver ${id}-vs -cipherName TLS1.3-AES256-GCM-SHA384
bind ssl vserver ${id}-vs -cipherName TLS1.3-CHACHA20-POLY1305-SHA256
bind ssl vserver ${id}-vs -cipherName TLS1.2-ECDHE-RSA-AES128-GCM-SHA256
bind ssl vserver ${id}-vs -cipherName TLS1.2-ECDHE-RSA-AES256-GCM-SHA384
bind ssl vserver ${id}-vs -cipherName TLS1.2-ECDHE-ECDSA-AES128-GCM-SHA256
bind ssl vserver ${id}-vs -cipherName TLS1.2-ECDHE-ECDSA-AES256-GCM-SHA384
add responder action ${id}-to-https redirect "\"https://$name\" + HTTP.REQ.URL.PATH_AND_QUERY" -responseStatusCode 301
add responder policy ${id}-to-https true ${id}-to-https
add lb vserver ${id}-vs-80 HTTP $vip $lpp
bind lb vserver ${id}-vs-80 -policyName ${id}-to-https -priority 100 -type REQUEST
# then on the Watchover server: ./watchover.sh edge lb <the NetScaler SNIP addresses>

EOF
}

tls12="ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
tls13="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256"

haproxy() {
cat <<EOF
# ---------------------------------------------------------------- HAProxy 2.x ---- $name -> $ip:$hp
# re-encrypt with X-Forwarded-For; /etc/haproxy/certs/$name.pem = certificate + chain + key; watchover-root.crt from the server's ./certs
frontend ${id}_https
    bind $vip:$lhp ssl crt /etc/haproxy/certs/$name.pem alpn h2,http/1.1 ssl-min-ver TLSv1.2 ciphers $tls12 ciphersuites $tls13
    mode http
    option forwardfor
    http-request set-header X-Forwarded-Proto https
    timeout client 300s
    default_backend ${id}
frontend ${id}_http
    bind $vip:$lpp
    mode http
    http-request redirect scheme https code 301
backend ${id}
    mode http
    option httpchk GET /healthz
    http-check send hdr Host $name
    http-check expect status 200
    timeout server 300s
    timeout tunnel 1h
    server ${id}1 $ip:$hp ssl verify required ca-file /etc/haproxy/watchover-root.crt sni str($name) check check-sni $name ssl-min-ver TLSv1.2 ciphers $tls12 ciphersuites $tls13
# then on the Watchover server: ./watchover.sh edge lb <the HAProxy address>

EOF
}

case "$vendor" in
  f5) f5 ;; netscaler|citrix|adc) netscaler ;; haproxy) haproxy ;; all) f5; netscaler; haproxy ;;
  *) echo "lb-config: vendor is f5, netscaler, haproxy or all" >&2; exit 2 ;;
esac
