#!/usr/bin/env bash
# The development-only parts of a source tree, removed from the copy that goes to a server (a setup file, a bundle): demo
# screenshots, the OpenShift chart, prompt notes, the test suite and the hackathon / assistant notes. The images never had them
# (.dockerignore); install-rhel.sh leaves the same paths out of /opt/watchover. tests/fixtures stays: the receiver's image build
# reads it. Usage: scripts/prune-tree.sh DIR
set -euo pipefail
d="${1:?usage: prune-tree.sh DIR}"
[ -f "$d/pyproject.toml" ] || { echo "prune-tree: $d is not a Watchover tree" >&2; exit 2; }
rm -rf "${d:?}/demo" "$d/deploy" "$d/prompts" "$d/AI_JURI.md" "$d/submission.json" "$d/CLAUDE.md"
if [ -d "$d/tests" ]; then find "$d/tests" -mindepth 1 -maxdepth 1 ! -name fixtures -exec rm -rf {} +; fi
