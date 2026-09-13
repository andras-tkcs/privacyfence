#!/usr/bin/env python3
"""Keep `main`'s required-status-checks list in sync with the `.github/workflows/tests.yml` jobs
that actually run on every PR and are meant to gate correctness (`docs/automated-test-strategy-
plan.md` Phase 11).

Branch protection is GitHub repo configuration this repo doesn't otherwise track as a file --
there's no commit history or diff to review for it, which is exactly why it silently falls behind
the workflow file as new jobs get added (a job promoted to per-PR in `tests.yml` doesn't, by
itself, make GitHub require it before a PR can merge). This script is the one place the *intended*
required set is written down, reviewable, and applied the same way every time, instead of an
implied setting someone edits once by hand in the web UI and never revisits.

**This talks to the repository *rulesets* API, not classic branch protection.** `main` is governed
by a repository ruleset (Settings -> Rules -> Rulesets), which is a different resource from the
classic `/branches/{branch}/protection` one an earlier version of this script used. The two are
evaluated together by GitHub but are stored separately, and the classic endpoint reports
`enforcement_level: "off"` with empty `contexts` on this repo *because no classic rule exists* --
not because nothing is enforcing. Reading the wrong one produces a confident false alarm: a code
review actually reported "branch protection is enforcing nothing" off exactly that response while
the ruleset was requiring all seven checks. If this script ever has to support a repo on classic
protection instead, add it as an explicit second code path -- do not quietly switch endpoints.

REQUIRED_STATUS_CHECKS below must be updated in the same PR as any change to which jobs
`tests.yml` runs on every PR, or to `test-python-compat`'s own Python-version matrix (each leg
reports as its own check, named from that job's `name:` template) -- see the comment above the
list for exactly what's included and why.

Usage:
    python scripts/update_branch_protection.py show
    python scripts/update_branch_protection.py apply [--dry-run]

Requires GITHUB_TOKEN in the environment: a token with admin rights on this repo's rulesets
(fine-grained "Administration: write", or classic `repo` scope on an org/repo admin's account).
Never run `apply` with a token you don't already trust to decide what blocks every future PR from
merging -- there is no CI job that runs this automatically, and there shouldn't be one: the target
list is a human, reviewed-in-PR decision, and applying it against the live repo is a separate,
deliberate step a maintainer takes after that PR merges.
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

# The rule type inside a ruleset that carries the required-status-checks list.
_RULE_TYPE = "required_status_checks"

# Fields GitHub returns on a ruleset but rejects (or ignores) on the update call -- stripped before
# PUTting the object back. Everything else is round-tripped verbatim so that updating the checks
# can't silently drop a rule, a bypass actor, or a condition this script doesn't model.
_READ_ONLY_RULESET_FIELDS = frozenset(
    {"id", "node_id", "source", "source_type", "created_at", "updated_at", "_links", "current_user_can_bypass"}
)

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
#
# Also deliberately NOT included, for a different reason: `lockfile-freshness`, `pip-audit` and
# `npm-audit` (dependency-audit.yml), and `verify` (deploy-download-worker.yml). All four are
# meant to block and do run on `pull_request`, but only when the PR touches one of their
# workflow's own `paths:` filters (a dependency manifest; `cloudflare/downloads/**`) -- not
# unconditionally on every PR. Requiring a `paths:`-filtered job by name would wedge any PR that
# doesn't touch those paths: GitHub never sees that context reported at all for such a PR, and a
# required check that never reports blocks the merge indefinitely. Making one of these genuinely
# gating needs a job that always runs and short-circuits when the paths don't match, so the
# context always reports -- not an entry in this list. Revisit if any of those workflows ever
# drops its `paths:` filter.
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
        raise SystemExit("GITHUB_TOKEN is required (a token with rulesets admin rights on this repo)")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _rulesets_url(owner: str, repo: str) -> str:
    return f"{API_ROOT}/repos/{owner}/{repo}/rulesets"


def _targets_branch(ruleset: dict[str, Any], branch: str) -> bool:
    """True if this ruleset's ref conditions include `branch`.

    Matches both the literal ref (`refs/heads/main`) and GitHub's `~DEFAULT_BRANCH` placeholder,
    which is how the UI's "Include default branch" option is stored -- a ruleset created that way
    never names the branch literally.
    """
    include = ruleset.get("conditions", {}).get("ref_name", {}).get("include", [])
    return f"refs/heads/{branch}" in include or "~DEFAULT_BRANCH" in include or "~ALL" in include


def find_branch_ruleset(owner: str, repo: str, branch: str) -> dict[str, Any] | None:
    """The active branch ruleset governing `branch`, fetched in full, or None if there isn't one.

    The list endpoint returns only a summary (no `rules`), so the match is made on the summary and
    the winner is then re-fetched by id for the rules themselves. Disabled (`enforcement:
    "disabled"`) rulesets are skipped -- they enforce nothing, so treating one as the live setting
    would be the same class of false reading this script's own docstring warns about.
    """
    resp = requests.get(_rulesets_url(owner, repo), headers=_headers(), timeout=30)
    resp.raise_for_status()
    candidates = [
        rs
        for rs in resp.json()
        if rs.get("target") == "branch" and rs.get("enforcement") != "disabled"
    ]
    for summary in candidates:
        detail = requests.get(f"{_rulesets_url(owner, repo)}/{summary['id']}", headers=_headers(), timeout=30)
        detail.raise_for_status()
        ruleset = detail.json()
        if _targets_branch(ruleset, branch):
            return ruleset
    return None


def required_checks_rule(ruleset: dict[str, Any]) -> dict[str, Any] | None:
    for rule in ruleset.get("rules", []):
        if rule.get("type") == _RULE_TYPE:
            return rule
    return None


def get_current(owner: str, repo: str, branch: str) -> dict[str, Any] | None:
    """The branch's current required-status-checks state, or None if nothing is requiring checks.

    Returns `{"contexts": [...], "strict": bool, "ruleset": {...}}`. None means either no active
    branch ruleset governs `branch`, or one does but carries no `required_status_checks` rule --
    `apply` distinguishes the two, since the first is not something this script should fix by
    inventing a ruleset.
    """
    ruleset = find_branch_ruleset(owner, repo, branch)
    if ruleset is None:
        return None
    rule = required_checks_rule(ruleset)
    if rule is None:
        return {"contexts": [], "strict": True, "ruleset": ruleset}
    params = rule.get("parameters", {})
    return {
        "contexts": [c["context"] for c in params.get("required_status_checks", [])],
        "strict": params.get("strict_required_status_checks_policy", True),
        "ruleset": ruleset,
    }


def apply_checks(owner: str, repo: str, ruleset: dict[str, Any], checks: list[str], *, strict: bool) -> None:
    """Replaces the ruleset's required-status-check contexts with exactly `checks`.

    The whole ruleset is PUT back, because that is the only update this API offers -- so everything
    this script does not manage (the `pull_request` rule, `deletion`, `non_fast_forward`, bypass
    actors, conditions) is round-tripped from the GET rather than re-specified here. An existing
    context's `integration_id` is preserved; a newly added one is sent without it, which GitHub
    resolves by name.
    """
    existing_integration = {
        c["context"]: c["integration_id"]
        for c in (required_checks_rule(ruleset) or {}).get("parameters", {}).get("required_status_checks", [])
        if c.get("integration_id") is not None
    }
    new_rule = {
        "type": _RULE_TYPE,
        "parameters": {
            "strict_required_status_checks_policy": strict,
            "do_not_enforce_on_create": (required_checks_rule(ruleset) or {})
            .get("parameters", {})
            .get("do_not_enforce_on_create", False),
            "required_status_checks": [
                {"context": name, **({"integration_id": existing_integration[name]} if name in existing_integration else {})}
                for name in sorted(checks)
            ],
        },
    }
    rules = [r for r in ruleset.get("rules", []) if r.get("type") != _RULE_TYPE] + [new_rule]
    body = {k: v for k, v in ruleset.items() if k not in _READ_ONLY_RULESET_FIELDS}
    body["rules"] = rules
    resp = requests.put(
        f"{_rulesets_url(owner, repo)}/{ruleset['id']}", headers=_headers(), json=body, timeout=30
    )
    resp.raise_for_status()


def _diff(current: list[str], target: list[str]) -> tuple[list[str], list[str]]:
    missing = sorted(set(target) - set(current))
    extra = sorted(set(current) - set(target))
    return missing, extra


_NO_RULESET = (
    "No active branch ruleset governs {branch!r} on {owner}/{repo}.\n"
    "This script manages required status checks inside a ruleset (Settings -> Rules -> Rulesets);\n"
    "it deliberately will not create one, and it does not read or write classic branch protection\n"
    "(Settings -> Branches) -- see this script's module docstring for why that distinction matters.\n"
    "Create the ruleset first, then re-run this."
)


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
    if current_state is None:
        print(_NO_RULESET.format(branch=args.branch, owner=args.owner, repo=args.repo), file=sys.stderr)
        return 1

    print(f"ruleset: {current_state['ruleset'].get('name')!r} (id {current_state['ruleset'].get('id')})")
    current = sorted(current_state["contexts"])
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
        # Preserve the ruleset's existing "strict" setting (require branches to be up to date
        # before merging) rather than silently changing it; default True if the ruleset carried no
        # required_status_checks rule at all, matching GitHub's own default for a new one.
        apply_checks(
            args.owner, args.repo, current_state["ruleset"], target, strict=current_state["strict"]
        )
        print("applied. new required status checks:", target)
        return 0

    return 1  # pragma: no cover -- unreachable, argparse enforces required=True above


if __name__ == "__main__":
    sys.exit(main())
