"""Tests for scripts/pre_release_check.py's version-consistency check --
the one piece of that script that's pure logic and worth covering offline.
The rest of the script just shells out to `pytest`/`npm`, which is already
exercised by CI running those suites directly."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pre_release_check as check  # noqa: E402


def _write_repo(tmp_path: Path, pyproject_version: str, package_version: str) -> Path:
    (tmp_path / "src" / "privacyfence").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "privacyfence"\nversion = "{pyproject_version}"\n'
    )
    (tmp_path / "src" / "privacyfence" / "__init__.py").write_text(
        f'__version__ = "{package_version}"\n'
    )
    return tmp_path


def test_matching_versions_pass(tmp_path):
    repo = _write_repo(tmp_path, "3.1.0", "3.1.0")
    assert check.check_version_consistency(repo) == []


def test_mismatched_versions_reported(tmp_path):
    repo = _write_repo(tmp_path, "3.1.0", "3.0.0")
    errors = check.check_version_consistency(repo)
    assert len(errors) == 1
    assert "3.1.0" in errors[0] and "3.0.0" in errors[0]


def test_missing_pyproject_version_reported(tmp_path):
    repo = _write_repo(tmp_path, "3.1.0", "3.1.0")
    (repo / "pyproject.toml").write_text('[project]\nname = "privacyfence"\n')
    errors = check.check_version_consistency(repo)
    assert any("pyproject.toml" in error for error in errors)


def test_missing_package_version_reported(tmp_path):
    repo = _write_repo(tmp_path, "3.1.0", "3.1.0")
    (repo / "src" / "privacyfence" / "__init__.py").write_text("")
    errors = check.check_version_consistency(repo)
    assert any("__init__.py" in error for error in errors)
