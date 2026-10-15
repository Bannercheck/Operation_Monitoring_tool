#!/bin/sh
# Watchover action template sap.wp.cancel (R3, irreversible; ships disabled until the customer's Basis team signs off).
# Refuses unless <pid> is a disp+work work process of <SID>/<NR> in the current ABAPGetWPTable output; the signal used is
# the one Basis validated for the kernel release (set in /etc/watchover/actions.d/sap-act.local, root-owned).
set -eu
sid="${1:?sid}"; nr="${2:?nr}"; pid="${3:?pid}"
conf=/etc/watchover/actions.d/sap-act.local
[ -f "$conf" ] || { echo "rejected: sap-act not validated by Basis" >&2; exit 3; }
sig=$(sed -n 's/^signal=//p' "$conf")
[ -n "$sig" ] || { echo "rejected: no validated signal" >&2; exit 3; }
/usr/sap/hostctrl/exe/sapcontrol -nr "$nr" -function ABAPGetWPTable -format script | grep -q "Pid: $pid\$" \
  || { echo "rejected: pid is not a work process of $sid/$nr" >&2; exit 4; }
[ "$(cat /proc/$pid/comm 2>/dev/null)" = "disp+work" ] || { echo "rejected: not disp+work" >&2; exit 4; }
kill "-$sig" "$pid"
echo "cancelled pid=$pid signal=$sig"
