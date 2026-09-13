#!/usr/bin/env python3
"""Keep `main`'s GitHub branch-protection required-status-checks list in sync with the
`.github/workflows/tests.yml` jobs that actually run on every PR and are meant to gate
correctness (`docs/automated-test-strategy-plan.md` Phase 11).

Branch protection is GitHub repo configuration this repo doesn't otherwise track as a file --
there's no commit history or diff to review for it, which is exactly why it silently falls behind
the workflow file as new jobs get added (a job promoted to per-PR in `tests.yml` doesn't, by
itself, make GitHub require it before a PR can merge). This script is the one place the *intended*
required set is written down, reviewable, and applied the same way every time, instead of an
implied setting someone edits once by hand in Settings -> Branches and never revisits.

REQUIRED_STATUS_CHECKS below must be updated in the same PR as any change to which jobs
`tests.yml` runs on every PR, or to `test-python-compat`'s own Python-version matrix (each leg
reports as its own check, named from that job's `name:` template) -- see the comment above the
list for exactly what's included and why.

Usage:
    python scripts/update_branch_protection.py show
    python scripts/update_branch_protection.py apply [--dry-run]

Requires GITHUB_TOKEN in the environment: a token with admin rights on this repo's branch
protection (fine-grained "Administration: write", or classic `repo` scope on an org/repo admin's
account). Never run `apply` with a token you don't already trust to decide what blocks every
future PR from merging -- there is no CI job that runs this automatically, and there shouldn't be
one: the target list is a human, reviewed-in-PR decision, and applying it against the live repo is
a separate, deliberate step a maintainer takes after that PR merges.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import requests

OWNER = "privacyfence"
REPO = "privacyfence"
BRANCH = "main"
API_ROOT = "https://api.github.com"

# Every job in tests.yml that runs on every PR (no job- or step-level `if:` restricts any of
# these to a schedule or a path) and is meant to gate correctness. Requiring a job by name
# requires its overall conclusion -- static-analysis's own mypy step still uses
# `continue-on-error: true` and so never turns the job red (Phase 11 exit criteria confirms this
# is a job-level, not step-level, mechanism), but its `ruff check .` and `bandit` steps are both
# blocking, so requiring the job means "require ruff and bandit" (mypy stays informational until
# it gets the same per-module treatment -- see [tool.mypy] in pyproject.toml).
#
# test-python-compat's matrix (.github/workflows/tests.yml's `python-version: ['3.11', '3.12']`)
# reports one check per leg, named from that job's own `name:` template -- both legs are listed
# individually below; add/remove an entry here if that matrix ever changes.
#
# Deliberately NOT included: the packaged-artifact jobs (Phase 6) and the graphical-session jobs
# (Phase 7) -- both live in build.yml / their own scheduled workflows and never run on
# `pull_request`, so they can't be a per-PR required check at all.
REQUIRED_STATUS_CHECKS = [
    "test",
    "platform-windows",
    "platform-macos",
    "Test (Python 3.11, core suite)",
    "Test (Python 3.12, core suite)",
    "static-analysis",
    "org-mode-smoke",
]


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required (a token with branch-protection admin rights on this repo)")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _required_status_checks_url(owner: str, repo: str, branch: str) -> str:
    return f"{API_ROOT}/repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks"


def get_current(owner: str, repo: str, branch: str) -> dict | None:
    """Returns the branch's current required_status_checks sub-resource (contexts + strict), or
    None if the branch has no protection rule -- or no required-status-checks configured -- at
    all."""
    resp = requests.get(_required_status_checks_url(owner, repo, branch), headers=_headers(), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def apply_checks(owner: str, repo: str, branch: str, checks: list[str], *, strict: bool) -> None:
    """Replaces the branch's required-status-check contexts with exactly `checks`. This PATCHes
    only the required_status_checks sub-resource, not the full protection rule -- it never touches
    required reviews, admin enforcement, or anything else already configured on the branch."""
    body: dict[str, Any] = {"strict": strict, "contexts": sorted(checks)}
    resp = requests.patch(_required_status_checks_url(owner, repo, branch), headers=_headers(), json=body, timeout=30)
    resp.raise_for_status()


def _diff(current: list[str], target: list[str]) -> tuple[list[str], list[str]]:
    missing = sorted(set(target) - set(current))
    extra = sorted(set(current) - set(target))
    return missing, extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner", default=OWNER)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--branch", default=BRANCH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="Print the branch's current vs. target required status checks")
    apply_parser = subparsers.add_parser("apply", help="Set the branch's required status checks to the target list")
    apply_parser.add_argument("--dry-run", action="store_true", help="Print what would change without applying it")

    args = parser.parse_args(argv)
    target = sorted(REQUIRED_STATUS_CHECKS)
    current_state = get_current(args.owner, args.repo, args.branch)
    current = sorted(current_state.get("contexts", [])) if current_state else []
    missing, extra = _diff(current, target)

    if args.command == "show":
        print("current:", current)
        print("target: ", target)
        if missing:
            print("missing (would be added by `apply`):", missing)
        if extra:
            print("extra (not in this script's target list -- review before removing):", extra)
        if not missing and not extra:
            print("already in sync")
        return 0

    if args.command == "apply":
        if not missing and not extra:
            print("already in sync -- nothing to do")
            return 0
        if missing:
            print("adding:", missing)
        if extra:
            print("removing:", extra)
        if args.dry_run:
            print("(dry run -- not applied)")
            return 0
        # Preserve the branch's existing "strict" setting (require branches to be up to date
        # before merging) rather than silently changing it; default True if protection didn't
        # exist yet, matching GitHub's own default for a new required_status_checks rule.
        strict = current_state.get("strict", True) if current_state else True
        apply_checks(args.owner, args.repo, args.branch, target, strict=strict)
        print("applied. new required status checks:", target)
        return 0

    return 1  # pragma: no cover -- unreachable, argparse enforces required=True above


if __name__ == "__main__":
    sys.exit(main())
