#!/usr/bin/env bash
# Watchover: why does a container stop with exit code 132 (illegal instruction, seen on an ARM VM)? Shows the CPU, the containers
# and their last log lines, then imports the dashboard image's native libraries one by one (numpy with and without
# OPENBLAS_CORETYPE, pandas, pyarrow, cryptography, psycopg, pydantic) so the one that dies is named. Changes nothing.
#   sudo bash scripts/cpu-check.sh
set -u
ENGINE=$(command -v podman || command -v docker) || { echo "podman / docker not found"; exit 1; }
IMG=$($ENGINE inspect watchover --format '{{.ImageName}}' 2>/dev/null || true)
[ -n "$IMG" ] || IMG=$($ENGINE images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E '(^|/)watchover:[0-9]' || true)
[ -n "$IMG" ] || { echo "no watchover image yet (install first)"; exit 1; }
echo "== host: $(uname -m), kernel $(uname -r), page size $(getconf PAGESIZE), image $IMG"
grep -m3 -E 'CPU implementer|CPU part|^Features|^flags' /proc/cpuinfo | cut -c1-200
echo "== containers"; $ENGINE ps -a --format '{{.Names}}  {{.Status}}'
for c in watchover watchover-keyd watchover-edge; do echo "== last lines: $c"; $ENGINE logs --tail 6 "$c" 2>&1 | cut -c1-200; done
echo "== imports in the image (exit 132 = illegal instruction)"
t() {
  local label="$1" out rc; shift
  out=$($ENGINE run --rm --entrypoint python "$@" 2>&1); rc=$?
  printf '  %-24s exit %-3s %s\n' "$label" "$rc" "$([ "$rc" = 132 ] && echo '<- illegal instruction' || printf '%s' "$out" | tail -1 | cut -c1-90)"
}
t python                         "$IMG" -c "print('ok')"
t numpy                          "$IMG" -c "import numpy; print(numpy.__version__)"
t "numpy, OPENBLAS ARMV8"        -e OPENBLAS_CORETYPE=ARMV8 "$IMG" -c "import numpy; print(numpy.__version__)"
t pandas                         "$IMG" -c "import pandas; print(pandas.__version__)"
t pyarrow                        "$IMG" -c "import pyarrow; print(pyarrow.__version__)"
t cryptography                   "$IMG" -c "import cryptography.hazmat.bindings._rust; print('ok')"
t psycopg                        "$IMG" -c "import psycopg; print(psycopg.__version__)"
t pydantic_core                  "$IMG" -c "import pydantic_core; print('ok')"
t watchover.act.keyd             "$IMG" -c "import watchover.act.keyd; print('ok')"
t watchover.api                  "$IMG" -c "import watchover.api; print('ok')"
t "watchover.api, OPENBLAS ARMV8" -e OPENBLAS_CORETYPE=ARMV8 "$IMG" -c "import watchover.api; print('ok')"
