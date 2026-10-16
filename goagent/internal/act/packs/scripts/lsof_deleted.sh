#!/bin/sh
# Watchover action template linux.fs.lsof_deleted (read-only): files deleted but still held open under a path.
# Output: one "pid comm bytes path" line per open deleted file, then "deleted_open_bytes=<n> files=<n>".
# Installed root-owned 0755 under /usr/lib/watchover/actions/; its sha256 is pinned in linux-diag.tmpl.json.
set -eu
root="${1:?path}"
total=0; n=0
for fd in /proc/[0-9]*/fd/*; do
  t=$(readlink "$fd" 2>/dev/null) || continue
  case "$t" in
    "$root"*" (deleted)")
      pid=${fd#/proc/}; pid=${pid%%/*}
      comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo "?")
      size=$(stat -L -c %s "$fd" 2>/dev/null || echo 0)
      echo "$pid $comm $size ${t% (deleted)}"
      total=$((total + size)); n=$((n + 1)) ;;
  esac
done
echo "deleted_open_bytes=$total files=$n"
