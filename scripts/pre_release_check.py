#!/usr/bin/env python3
"""Automated gate to run before starting `docs/manual-pre-release-test-plan.md`.

This does not replace that plan -- it only covers the parts a release should
never be blocked on discovering *manually*: the same automated suite CI runs
(`docs/testing-policy.md` §1), plus a check that the version string is
actually consistent across the two files that must always agree (see this
repo's `CLAUDE.md` "Version bumps" section). Everything in
`manual-pre-release-test-plan.md` -- live fixtures, popup smoke, real
Cowork/Desktop prompts, DMG install -- still needs a human, a screen, and
real accounts, none of which this script has.

    .venv/bin/python scripts/pre_release_check.py

Run from the repo root (with `bridge/` node_modules already installed via
`npm install`), the same as CI. Exits non-zero if any check fails.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_pyproject_version(repo_root: Path) -> str | None:
    text = (repo_root / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def read_package_version(repo_root: Path) -> str | None:
    text = (repo_root / "src" / "privacyfence" / "__init__.py").read_text()
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def check_version_consistency(repo_root: Path) -> list[str]:
    """Returns a list of error strings; empty means the version is consistent."""
    errors = []
    pyproject_version = read_pyproject_version(repo_root)
    package_version = read_package_version(repo_root)
    if pyproject_version is None:
        errors.append("pyproject.toml: could not find [project].version")
    if package_version is None:
        errors.append("src/privacyfence/__init__.py: could not find __version__")
    if pyproject_version is not None and package_version is not None and pyproject_version != package_version:
        errors.append(
            f"version mismatch: pyproject.toml has {pyproject_version!r}, "
            f"src/privacyfence/__init__.py has {package_version!r}"
        )
    return errors


def run(description: str, cmd: list[str], cwd: Path) -> bool:
    print(f"--- {description} ({' '.join(cmd)}) ---")
    result = subprocess.run(cmd, cwd=cwd)
    ok = result.returncode == 0
    print(f"--- {description}: {'PASS' if ok else 'FAIL'} ---\n")
    return ok


def main() -> int:
    results: dict[str, bool] = {}

    version_errors = check_version_consistency(REPO_ROOT)
    for error in version_errors:
        print(f"VERSION CHECK: {error}")
    results["version consistency"] = not version_errors

    results["pytest"] = run(
        "pytest",
        ["python3", "-m", "pytest", "-v", "--cov=src/privacyfence", "--cov-report=term-missing"],
        cwd=REPO_ROOT,
    )
    results["bridge npm test"] = run("bridge npm test", ["npm", "test"], cwd=REPO_ROOT / "bridge")
    results["bridge typecheck"] = run(
        "bridge typecheck", ["npm", "run", "typecheck"], cwd=REPO_ROOT / "bridge"
    )

    print("=== Pre-release check summary ===")
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    if not all(results.values()):
        print(
            "\nFix the failures above before starting "
            "docs/manual-pre-release-test-plan.md."
        )
        return 1

    print(
        "\nAll automated checks passed. Continue with "
        "docs/manual-pre-release-test-plan.md for the manual sections "
        "(fixture freshness, popup smoke, live QA prompt, DMG install) "
        "before cutting the release."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
