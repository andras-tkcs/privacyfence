#!/usr/bin/env bash
# Regenerate requirements/*.lock.txt from pyproject.toml -- see
# requirements/README.md for what these two files are and why they're
# hash-locked (SEC-19, Phase 2.4 in docs/security-remediation-plan.md).
#
# Prerequisites: pip install pip-tools (kept out of the `dev` extra itself
# so that installing `.[dev]` doesn't pull in a tool only this script uses).
#
# Usage: ./scripts/update_dependency_locks.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v pip-compile &>/dev/null; then
  echo "pip-compile not found -- run: pip install pip-tools" >&2
  exit 1
fi

# --allow-unsafe: without it, pip-compile refuses to pin packages like
# setuptools that pip normally treats specially, but --generate-hashes
# requires *every* installed package to be pinned with a hash -- including
# those. --generate-hashes: the whole point of this file (see module
# docstring above).
pip-compile --generate-hashes --allow-unsafe \
  --output-file=requirements/runtime.lock.txt \
  pyproject.toml

pip-compile --generate-hashes --allow-unsafe \
  --extra dev --extra test \
  --output-file=requirements/dev.lock.txt \
  pyproject.toml

echo "Regenerated requirements/runtime.lock.txt and requirements/dev.lock.txt."
