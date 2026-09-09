#!/usr/bin/env python3
"""Coverage ratchet (TST-03, docs/security-remediation-plan.md Phase 2.1).

`pytest`'s own `--cov-report=term-missing` (docs/testing-policy.md §1) is
informational only -- nothing before this script gated a merge on coverage
actually staying where it was, so a PR could silently drop coverage on a
security-critical module (an untested new branch in gate.py, an
exception-handling path in oauth_provider.py nothing exercises) and CI would
still go green. This script is the gate: it reads the `coverage.json` report
`pytest --cov-report=json:coverage.json` produces and fails if:

  * overall branch+line coverage (`totals.percent_covered` -- the same
    combined metric pytest-cov's own summary line reports) drops below
    OVERALL_FLOOR, or
  * any module in MODULE_FLOORS drops below its own, individually higher,
    floor.

MODULE_FLOORS exists because a single overall floor doesn't protect any one
file -- gate.py is ~2% of this package's statements, so a regression there
can hide inside the aggregate. The modules listed are the ones Phase 0/1 of
the remediation plan (SEC-01 through SEC-15) touched or added: the URL/
scheme allowlist, identity-matching, audit-export, org-config/bundle-trust,
browser-session and token-lifetime, privacy-filter fail-closed, secure-write,
OIDC-discovery-trust, MCP-boundary-error-taxonomy, and per-principal-cap
code paths.

Every floor below is this script's *initial* value: the module's actual
branch+line coverage the day this ratchet was turned on (2026-09-09,
`pytest -v --cov=src/privacyfence --cov-branch`, full suite, 4584 tests),
rounded down to the nearest whole percent as headroom against harmless
float jitter. A floor only ever moves in one direction, deliberately: if a
PR's own new tests raise a module's coverage, bump that module's number up
in the same PR (that's the point of a ratchet -- it should be raised often,
by whoever earns it); if a PR needs to lower one, that's a real coverage
regression and belongs in the PR description, not a silent edit here.

Usage (same as CI -- see .github/workflows/tests.yml and
scripts/pre_release_check.py):

    pytest --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json
    python scripts/check_coverage_floor.py coverage.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Combined (line + branch) percentage, matching coverage.json's
# totals.percent_covered / pytest-cov's own summary "Cover" column.
OVERALL_FLOOR = 94.0

# Security-critical modules get a floor of their own, on top of the overall
# one above -- see the module docstring for why. Paths are repository-
# relative, matching coverage.json's "files" keys exactly (pytest run from
# REPO_ROOT, as CI and pre_release_check.py both do).
MODULE_FLOORS: dict[str, float] = {
    # SEC-01: shared href-scheme allowlist and its two callers.
    "src/privacyfence/url_safety.py": 100.0,
    "src/privacyfence/email_markdown.py": 99.0,
    "src/privacyfence/markdown_to_html.py": 95.0,
    # Core gate/auto-accept/audit path.
    "src/privacyfence/gate.py": 91.0,
    "src/privacyfence/auto_accept.py": 94.0,  # SEC-02 identity rules live here
    "src/privacyfence/audit_log.py": 95.0,  # SEC-03 formula-injection guard
    "src/privacyfence/approvals.py": 92.0,  # SEC-15 per-principal cap
    "src/privacyfence/pii_detector.py": 98.0,
    # SEC-04: org-config fail-closed load path.
    "src/privacyfence/org_mode.py": 100.0,
    "src/privacyfence/daemon_main.py": 96.0,
    # SEC-05: org bundle trust.
    "src/privacyfence/org_bundle_signing.py": 98.0,
    # 91.0, not the ~93.2% the rest of this module would suggest: whether
    # coverage sees _resolve_names_async()'s done() callback body (fired
    # from a background thread started by _run_async(), not on the test's
    # own thread) depends on that thread's scheduling relative to the test
    # finishing -- observed at both 93.18% (this floor's laptop/local runs)
    # and 92.87% (CI, same commit) across otherwise-identical runs. A real
    # fix is a deterministic wait on that thread rather than a wider floor
    # (TST-11, docs/security-remediation-plan.md Phase 3.12); until then
    # this floor carries enough headroom not to flake red on that one line.
    "src/privacyfence/settings_controller.py": 91.0,
    # SEC-06/SEC-12/SEC-13: bootstrap flow, session and token lifetimes.
    "src/privacyfence/web/oauth_provider.py": 99.0,
    "src/privacyfence/web/org_session.py": 100.0,
    "src/privacyfence/web/session_auth.py": 100.0,
    # SEC-07: privacy-filter fail-closed load path.
    "src/privacyfence/privacy_filter.py": 100.0,
    # SEC-09: atomic, permission-safe credential/config writes.
    "src/privacyfence/secure_files.py": 100.0,
    # SEC-11: OIDC discovery trust validation.
    "src/privacyfence/org_identity.py": 100.0,
    "src/privacyfence/web/routes_org_identity.py": 99.0,
    # SEC-10: safe-error taxonomy at the MCP boundary.
    "src/privacyfence/safe_errors.py": 100.0,
    "src/privacyfence/web/routes_mcp.py": 100.0,
    # CSRF/Origin/step-up auth for write approvals.
    "src/privacyfence/web/routes_security.py": 96.0,
    "src/privacyfence/webauthn_stepup.py": 98.0,
}


def _load_totals(cov_json: dict) -> tuple[float, dict[str, float]]:
    overall = cov_json["totals"]["percent_covered"]
    per_file = {path: data["summary"]["percent_covered"] for path, data in cov_json["files"].items()}
    return overall, per_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "coverage_json",
        nargs="?",
        default="coverage.json",
        help="Path to the coverage.json report from `--cov-report=json:...` (default: coverage.json)",
    )
    args = parser.parse_args(argv)

    cov_path = Path(args.coverage_json)
    if not cov_path.is_file():
        print(
            f"error: {cov_path} not found -- run pytest with "
            "--cov=src/privacyfence --cov-branch --cov-report=json:coverage.json first",
            file=sys.stderr,
        )
        return 2

    cov_json = json.loads(cov_path.read_text())
    overall, per_file = _load_totals(cov_json)

    failures: list[str] = []

    print(f"Overall coverage: {overall:.2f}% (floor {OVERALL_FLOOR:.1f}%)")
    if overall < OVERALL_FLOOR:
        failures.append(f"overall coverage {overall:.2f}% is below the {OVERALL_FLOOR:.1f}% floor")

    print("\nSecurity-critical module floors:")
    for module, floor in sorted(MODULE_FLOORS.items()):
        actual = per_file.get(module)
        if actual is None:
            # A module in this list was renamed, moved, or deleted without
            # updating MODULE_FLOORS -- that silently drops its floor
            # enforcement, so treat it as a failure rather than skipping it.
            print(f"  [MISSING] {module} (floor {floor:.1f}%) -- not present in {cov_path}")
            failures.append(
                f"{module} is in MODULE_FLOORS but has no coverage data in {cov_path} "
                "(renamed, moved, or deleted? update this script's MODULE_FLOORS)"
            )
            continue
        ok = actual >= floor
        status = "ok" if ok else "FAIL"
        print(f"  [{status:4}] {module}: {actual:.2f}% (floor {floor:.1f}%)")
        if not ok:
            failures.append(f"{module} coverage {actual:.2f}% is below its {floor:.1f}% floor")

    if failures:
        print("\nCoverage ratchet failed:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nAdd or strengthen tests to get back above the floor, rather than "
            "lowering the floor in scripts/check_coverage_floor.py -- see this "
            "script's module docstring."
        )
        return 1

    print("\nCoverage ratchet passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
