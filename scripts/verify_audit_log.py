#!/usr/bin/env python3
"""Verify the append-integrity hash chain on an installed audit log (SEC-23,
docs/security-remediation-plan.md, Phase 3 item 3.6).

Checks every ``*.jsonl`` file under an audit log directory with
``AuditLogger.verify_chain()`` (src/privacyfence/audit_log.py) -- each
entry's HMAC-SHA256 ``entry_hash`` must recompute correctly from its own
stored fields (nothing was edited after PrivacyFence wrote it), and each
entry's ``prev_hash`` must match the previous entry's ``entry_hash``
(nothing was inserted, removed, or reordered). Uses the *same* chain key
(``.audit_chain.key``) the daemon itself uses -- reusing ``AuditLogger``
rather than reimplementing the hash logic here keeps the two from ever
silently drifting apart on what "canonical" means.

Needs PrivacyFence's own package installed (unlike build_org_bundle.py, this
imports audit_log.py directly rather than reimplementing its logic).

Usage:
    python3 scripts/verify_audit_log.py ~/.privacyfence/logs/audit
    python3 scripts/verify_audit_log.py ~/.privacyfence/logs/audit --week 2026-W28

Exit status is 0 when every checked week's chain is intact, 1 if any week
fails verification (or the directory has no *.jsonl files to check at all),
and 2 for a usage error (a missing/inaccessible directory).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from privacyfence.audit_log import AuditLogger  # noqa: E402


def _weeks_in(log_dir: Path) -> list[str]:
    return sorted(p.stem for p in log_dir.glob("*.jsonl"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_audit_log.py",
        description="Verify SEC-23's append-integrity hash chain on an installed audit log.",
    )
    parser.add_argument(
        "log_dir", help='Audit log directory, e.g. "~/.privacyfence/logs/audit" (local mode) or '
                        'a principal\'s own "logs/audit" under an org-mode server\'s data '
                        'directory. This is the same directory AuditLogger writes to -- see '
                        "docs/TECHNICAL_REFERENCE.md's \"Audit log\" section.",
    )
    parser.add_argument(
        "--week", metavar="YYYY-WNN", action="append", dest="weeks", default=None,
        help="Check only this ISO week (e.g. 2026-W28). Repeatable. Default: every *.jsonl file "
             "found in log_dir.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = Path(os.path.expanduser(args.log_dir))
    if not log_dir.is_dir():
        print(f"error: {log_dir} is not a directory", file=sys.stderr)
        return 2

    weeks = args.weeks if args.weeks else _weeks_in(log_dir)
    if not weeks:
        print(f"No *.jsonl files found under {log_dir} -- nothing to verify.")
        return 1

    # AuditLogger's own constructor generates a fresh chain key when none
    # exists (the right behavior for a daemon starting up for the first
    # time) -- but this is a read-only verification tool, and doing that
    # here would both mutate a directory that isn't its to write to and
    # verify every real entry against a key that was never the one used to
    # write them, producing a misleading "entry_hash does not match" for
    # every single entry instead of a clear "no key here to verify against".
    key_path = log_dir / ".audit_chain.key"
    if not key_path.exists():
        print(f"error: no chain key ({key_path}) in this directory -- nothing to verify against.", file=sys.stderr)
        return 2

    logger = AuditLogger(str(log_dir))
    all_ok = True
    for week in weeks:
        result = logger.verify_chain(week)
        status = "OK" if result.ok else "FAILED"
        print(f"{week}: {status} ({result.entries_checked} entries checked) -- {result.detail}")
        if not result.ok:
            print(f"  first broken entry: line {result.first_break_line}")
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
