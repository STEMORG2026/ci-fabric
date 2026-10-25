#!/bin/sh
# Reproducible developer environment bootstrap. Network access is needed only
# for the first dependency retrieval; every requirement is pinned and hashed.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON=${PYTHON:-python3.11}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python3
fi

if [ ! -x .venv/bin/python ]; then
  echo "creating .venv with $PYTHON"
  "$PYTHON" -m venv .venv
fi

if [ ! -f requirements-dev.lock ]; then
  echo "requirements-dev.lock is missing; regenerate it with uv:" >&2
  echo "  uv pip compile --generate-hashes requirements-dev.in -o requirements-dev.lock" >&2
  exit 3
fi

echo "installing hashed development toolchain"
.venv/bin/python -m pip install --disable-pip-version-check --require-hashes \
  -r requirements-dev.lock

echo "installing ci-fabric in editable mode"
.venv/bin/python -m pip install --disable-pip-version-check --no-deps \
  --no-build-isolation -e .

echo "ready: .venv/bin/python -m ci_fabric doctor"
