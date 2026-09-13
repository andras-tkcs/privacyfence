#!/usr/bin/env bash
# Regenerate requirements/*.lock.txt from pyproject.toml -- see
# requirements/README.md for what these two files are and why they're
# hash-locked (SEC-19, Phase 2.4).
#
# Prerequisites: python3.13 -m pip install pip-tools -- pip-tools itself
# kept out of the `dev` extra so that installing `.[dev]` doesn't pull in a
# tool only this script uses; Python 3.13 specifically, not "whatever's on
# PATH" -- see the version check below for why that's not a suggestion.
#
# Usage: ./scripts/update_dependency_locks.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# pip-compile resolves environment markers (e.g. a package's
# `typing-extensions; python_version < "3.13"`-style conditional
# dependencies) against the interpreter it runs under, not against
# pyproject.toml's `requires-python` floor -- so a lock file compiled with a
# different minor version than CI uses can end up with a different
# dependency set even though pyproject.toml didn't change at all, and
# dependency-audit.yml's lockfile-freshness job (which compiles under 3.13,
# matching every other job in that workflow and in tests.yml) would then
# flag a perfectly good regeneration as stale. Pin this script to that same
# version rather than "whatever pip-compile happens to be on PATH" so a
# local regeneration always matches what CI checks it against.
compiler="$(command -v python3.13 || true)"
if [ -z "$compiler" ]; then
  echo "python3.13 not found -- install it (dependency-audit.yml and tests.yml both compile/check requirements/*.lock.txt under 3.13) and retry, e.g.:" >&2
  echo "  python3.13 -m venv /tmp/lock-venv && /tmp/lock-venv/bin/pip install pip-tools && PATH=\"/tmp/lock-venv/bin:\$PATH\" $0" >&2
  exit 1
fi
if ! "$compiler" -c 'import piptools' &>/dev/null; then
  echo "pip-tools isn't installed for python3.13 -- run: python3.13 -m pip install pip-tools" >&2
  exit 1
fi

# --allow-unsafe: without it, pip-compile refuses to pin packages like
# setuptools that pip normally treats specially, but --generate-hashes
# requires *every* installed package to be pinned with a hash -- including
# those. --generate-hashes: the whole point of this file (see module
# docstring above).
#
# --upgrade: without it, pip-compile treats an existing output file as a
# constraint and keeps whatever it already has pinned, resolving a newer
# transitive dependency only when something *forces* it to -- it does NOT
# re-check whether a newer compatible release has shipped for a package
# that's already pinned. dependency-audit.yml's lockfile-freshness job
# compiles into a scratch file that doesn't exist yet, so it always gets a
# true from-scratch resolve against whatever's on PyPI right now; without
# --upgrade here, a plain rerun of this script against the already-
# committed lock files would silently drift behind that and get flagged as
# stale on the very next dependency-audit.yml run, even with no
# pyproject.toml change at all -- exactly the trap this script exists to
# prevent.
"$compiler" -m piptools compile --generate-hashes --allow-unsafe --upgrade \
  --output-file=requirements/runtime.lock.txt \
  pyproject.toml

"$compiler" -m piptools compile --generate-hashes --allow-unsafe --upgrade \
  --extra dev --extra test --extra lint \
  --output-file=requirements/dev.lock.txt \
  pyproject.toml

echo "Regenerated requirements/runtime.lock.txt and requirements/dev.lock.txt."
