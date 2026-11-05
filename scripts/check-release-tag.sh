#!/bin/sh
# Fail unless the tag being released matches VERSION (vX.Y.Z).
#
# The release workflow sets TAG from github.ref_name. Keeping the check in a
# script makes it runnable locally and testable without a tag push.
set -eu

ROOT=${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
VERSION_FILE="$ROOT/VERSION"

if [ ! -f "$VERSION_FILE" ]; then
    printf 'check-release-tag: missing VERSION file: %s\n' "$VERSION_FILE" >&2
    exit 1
fi

VERSION=$(cat "$VERSION_FILE")
TAG=${TAG:-}
if [ -z "$TAG" ]; then
    printf 'check-release-tag: TAG environment variable is required\n' >&2
    exit 1
fi

EXPECTED="v$VERSION"
if [ "$TAG" != "$EXPECTED" ]; then
    printf 'check-release-tag: tag %s does not match VERSION %s (expected %s)\n' \
        "$TAG" "$VERSION" "$EXPECTED" >&2
    exit 1
fi

printf 'check-release-tag: %s matches VERSION %s\n' "$TAG" "$VERSION"
