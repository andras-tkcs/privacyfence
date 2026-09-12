"""Shared CI-diagnostics capture for system/packaged-artifact test failures
(docs/automated-test-strategy-plan.md Phase 10).

Phase 10's objective is "make test failures diagnosable entirely from cloud
CI" -- this project is developed cloud-first, with no dedicated physical
test machines to re-run a failing macOS/Windows/Linux packaged-artifact test
on by hand. Every test this phase covers (``pytest.mark.packaged``,
``pytest.mark.system``) already isolates its own state -- daemon home
directory, install directory, audit log -- under pytest's own per-test
``tmp_path``, so the content worth capturing already exists on disk, at a
known location, the moment a test fails. There's no separate log-forwarding
mechanism to build; only a hook (``../conftest.py``'s own
``pytest_runtest_makereport``, which calls ``capture_failure_diagnostics``
below) that runs once per failing test and:

1. writes a small ``environment.txt`` -- OS/runtime versions and the CI run
   identifier (item 1's "OS/runtime versions, and a test-run identifier"),
2. writes ``manifest.txt``, a flat recursive listing (relative path + size,
   never content) of everything under that test's own ``tmp_path`` -- covers
   item 1's "an installed-file manifest (packaged tests)" without also
   re-uploading the installed binaries themselves as CI artifacts (see
   ``capture_directory_manifest``'s own docstring for why this is a listing,
   not a copy -- a PyInstaller onedir build or an extracted ``.deb`` easily
   runs to hundreds of MB),
3. copies out the small text logs a manifest listing alone wouldn't let
   anyone actually read -- ``daemon.log``/``install*.log``/
   ``uninstall*.log`` and any ``*.jsonl`` audit log, wherever they land
   under that ``tmp_path``. Every module this phase covers already names its
   own subprocess log exactly one of these ways (e.g.
   test_windows_packaged_smoke.py's ``_running_daemon``, test_deb_packaged_
   lifecycle.py's identically-shaped helper, tests/system/
   test_local_mode_system.py's ``_prepare_sandbox``) -- nothing here
   invents a new naming convention, it just collects the ones that already
   exist.

Nothing is written on a passing run -- ``TEST_RESULTS_ROOT`` never even gets
created unless a covered test actually fails -- matching the posture
test_browser_smoke.py's own Phase 4.5 fixture already established for the
browser suite, which this module deliberately leaves untouched: its own
screenshot/DOM/console capture already satisfies this phase's "browser
console... reuse Phase 4's existing capture", so nothing here duplicates it.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import sys
import uuid
from pathlib import Path

#: Every suite this phase covers writes under one root. Each CI job's own
#: "Upload failure diagnostics" step (.github/workflows/tests.yml,
#: build.yml, linux-graphical-session.yml, windows-graphical-session.yml)
#: uploads this whole tree with `if: failure()` / `if-no-files-found:
#: ignore`, `retention-days: 30` -- the same bounded-retention convention
#: connector-live-check.yml's own report upload already established
#: (item 3's "bounded retention... matching the existing pattern").
TEST_RESULTS_ROOT = Path(__file__).resolve().parent.parent / "test-results"

# Filenames (matched against a file's own name, not its full path) worth
# copying out in full -- small, plain text, and not otherwise readable from
# the manifest listing alone.
_LOG_NAME_PATTERNS = (
    "daemon.log",
    "install*.log",
    "install-*.log",
    "uninstall*.log",
    "*.jsonl",
)


def test_run_id() -> str:
    """A stable per-CI-run identifier: ``GITHUB_RUN_ID``/``GITHUB_RUN_
    ATTEMPT`` (both always set by Actions, and stable across a manual
    re-run of the same attempt, unlike e.g. a freshly generated uuid) when
    actually running in CI, else a short random id -- this still has to
    work for a contributor reproducing a failure locally, not just in CI.
    """
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        return f"{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{uuid.uuid4().hex[:8]}"


def _sanitize(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text)


def _write_text(dest: Path, content: str) -> None:
    """Best-effort: diagnostics capture must never itself raise and mask
    the test's own real failure (same posture test_browser_smoke.py's own
    ``_capture_failure_artifacts`` already takes for its screenshot/DOM
    writes)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    except OSError as exc:  # pragma: no cover -- best-effort diagnostics
        try:
            dest.with_name(dest.name + ".capture-error.txt").write_text(str(exc), encoding="utf-8")
        except OSError:
            pass


def capture_directory_manifest(root: Path, dest: Path) -> None:
    """A flat ``<relative path>\\t<size in bytes>`` listing of every file
    under ``root`` -- not a copy of their contents. This is what lets a
    failed packaged-artifact test's diagnostics answer "was X actually
    installed, and how big is it" without this phase's own CI artifact
    upload also re-shipping the installed binaries it's listing."""
    lines: list[str] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    size = -1
                lines.append(f"{path.relative_to(root)}\t{size}")
    _write_text(dest, "\n".join(lines) + ("\n" if lines else ""))


def copy_named_logs(root: Path, dest_dir: Path) -> list[Path]:
    """Copies every file under ``root`` whose own name matches one of
    ``_LOG_NAME_PATTERNS`` into ``dest_dir``, flattening each one's path
    (relative to ``root``) into its destination filename so two same-named
    logs from two different sub-homes in the same test (e.g. an N and an
    N+1 upgrade boot) don't collide."""
    copied: list[Path] = []
    if not root.is_dir():
        return copied
    seen: set[Path] = set()
    for pattern in _LOG_NAME_PATTERNS:
        for path in root.rglob(pattern):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            dest = dest_dir / _sanitize(str(path.relative_to(root)))
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
                copied.append(dest)
            except OSError as exc:  # pragma: no cover -- best-effort diagnostics
                _write_text(dest.with_name(dest.name + ".capture-error.txt"), str(exc))
    return copied


def write_environment_info(dest: Path) -> None:
    lines = [
        f"test_run_id={test_run_id()}",
        f"platform={platform.platform()}",
        f"python={sys.version.split()[0]}",
        f"hostname={socket.gethostname()}",
    ]
    _write_text(dest, "\n".join(lines) + "\n")


def suite_name_for(file_path: str) -> str:
    """Derives a stable, filesystem-friendly suite slug from a test module's
    own path -- e.g. ``tests/integration/test_deb_packaged_lifecycle.py`` ->
    ``deb-packaged-lifecycle``. The one thing every consumer of
    ``failure_dir``/``capture_failure_diagnostics`` must agree on, so a test
    module capturing something extra of its own (e.g.
    test_deb_packaged_lifecycle.py's ``dpkg -L`` manifest -- real installed
    system state ``tmp_path`` never isolates, so it needs its own capture
    call) lands in the exact same directory as this module's own
    ``environment.txt``/``manifest.txt``/``logs/``, not a sibling one."""
    return Path(file_path).stem.removeprefix("test_").replace("_", "-")


def failure_dir(nodeid: str, *, suite: str) -> Path:
    """The directory a given failing test's diagnostics live under --
    exposed separately from ``capture_failure_diagnostics`` so a test
    module can add its own extra artifact (see ``suite_name_for``'s own
    docstring) to the same place without recomputing this path by hand."""
    return TEST_RESULTS_ROOT / suite / _sanitize(nodeid)


def capture_failure_diagnostics(nodeid: str, tmp_path: Path, *, suite: str) -> Path:
    """The one entry point ``../conftest.py``'s own
    ``pytest_runtest_makereport`` hook calls for a failing
    ``packaged``/``system``-marked test. Always writes at least
    ``environment.txt`` -- even if ``tmp_path`` itself is already gone by
    teardown time, this still records the run id and platform, so a failure
    that already cleaned up after itself is never diagnosed as "nothing
    captured"."""
    dest = failure_dir(nodeid, suite=suite)
    write_environment_info(dest / "environment.txt")
    capture_directory_manifest(tmp_path, dest / "manifest.txt")
    copy_named_logs(tmp_path, dest / "logs")
    return dest
