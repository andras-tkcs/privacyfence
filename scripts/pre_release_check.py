#!/usr/bin/env python3
"""Automated gate to run before starting `docs/manual-pre-release-test-plan.md`.

This does not replace that plan -- it only covers the parts a release should
never be blocked on discovering *manually*: the same automated suite CI runs
(`docs/testing-policy.md` §1). Everything in `manual-pre-release-test-plan.md`
-- live fixtures, popup smoke, real Cowork/Desktop prompts, DMG install --
still needs a human, a screen, and real accounts, none of which this script
has.

There used to be a version-consistency check here too, comparing
`pyproject.toml`'s `project.version` against `src/privacyfence/__init__.py`'s
`__version__` -- both hand-bumped in the same commit. Neither of those static
strings exists any more (see this repo's CLAUDE.md "Releasing" section):
`__version__` is derived at import time from the git tag via setuptools_scm
(`src/privacyfence/__init__.py`), so there is exactly one source of truth
left and nothing to compare.

    .venv/bin/python scripts/pre_release_check.py

Run from the repo root (with `mcpb/shim/` node_modules already installed via
`npm install`), the same as CI. Exits non-zero if any check fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(description: str, cmd: list[str], cwd: Path) -> bool:
    print(f"--- {description} ({' '.join(cmd)}) ---")
    result = subprocess.run(cmd, cwd=cwd)
    ok = result.returncode == 0
    print(f"--- {description}: {'PASS' if ok else 'FAIL'} ---\n")
    return ok


def main() -> int:
    results: dict[str, bool] = {}

    results["pytest"] = run(
        "pytest",
        ["python3", "-m", "pytest", "-v", "--cov=src/privacyfence", "--cov-report=term-missing"],
        cwd=REPO_ROOT,
    )
    results["shim npm test"] = run("shim npm test", ["npm", "test"], cwd=REPO_ROOT / "mcpb" / "shim")
    results["shim typecheck"] = run(
        "shim typecheck", ["npm", "run", "typecheck"], cwd=REPO_ROOT / "mcpb" / "shim"
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
