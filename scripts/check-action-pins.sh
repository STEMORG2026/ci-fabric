#!/bin/sh
# Gate: every external action `uses:` must pin a full 40-hex commit SHA.
# Mutable tags and branches silently change what CI executes; SHAs do not.
# Local actions (uses: ./path) are pinned by the repository itself and pass.
# Usage: check-action-pins.sh [workflows-dir]   (default: .github/workflows)
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIR=${1:-"$ROOT/.github/workflows"}

violations=$(
  for f in "$DIR"/*.yml "$DIR"/*.yaml; do
    [ -f "$f" ] || continue
    awk -v file="$f" '
      /^[[:space:]]*(-[[:space:]]*)?uses:/ {
        line = $0
        sub(/^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*/, "", line)
        sub(/[[:space:]]*#.*$/, "", line)
        gsub(/[\042\047]/, "", line)
        sub(/[[:space:]]+$/, "", line)
        if (line ~ /^\.\//) next
        at = index(line, "@")
        if (at > 0) {
          ref = substr(line, at + 1)
          if (length(ref) == 40 && ref ~ /^[0-9a-f]+$/) next
        }
        printf "%s:%d: %s\n", file, NR, line
      }
    ' "$f"
  done
)

if [ -n "$violations" ]; then
  echo "unpinned action references (must be owner/repo@<40-hex commit SHA>):" >&2
  echo "$violations" >&2
  exit 1
fi
echo "action pins: all external uses are full commit SHAs"
