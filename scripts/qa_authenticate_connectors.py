#!/usr/bin/env python3
"""Runs the local, per-connector OAuth authentication steps that
`docs/connector-live-check-setup.md` Phase A calls for once per service
(A.1.8 Google, A.2.7 Slack, A.3.5 Atlassian, A.4.6 Salesforce) -- so instead
of typing up to nine separate `privacyfence-app --<connector>-oauth`
commands by hand against the four dedicated QA accounts from Phase A.1-A.4,
one command drives all of them in order.

This is purely an orchestration wrapper around the daemon's own OAuth
entrypoints (`daemon_main.parse_args`'s `--<connector>-oauth` flags) -- it
does not talk to any provider itself. Each step still opens a real browser
tab and needs you to click "Allow"/"Accept" there; nothing here is
unattended. Run it on a developer machine only, the same posture as
`scripts/qa_fixture_recorder.py` -- never in CI, never with a credential
provisioned to GitHub Actions or any other cloud service.

Telegram is deliberately not included: Phase A's dedicated-account budget
and exit criteria (A.5) cover Google, Slack, Atlassian, and Salesforce only
-- Telegram authenticates against your own already-existing personal
account (`docs/telegram-setup.md`), not a Phase A test account, and uses a
different flag (`--telegram-setup`) with a different (phone/code) prompt
shape. Run that one by hand if/when you need it.

Prerequisites (see `docs/qa-environment-setup.md`'s "Prerequisites"
checklist and `connector-live-check-setup.md` Phase A.1-A.4, steps 1-7 of
each): the org config bundle (`org_config.qa.json`, built by
`scripts/build_org_bundle.py` per A.1.7/A.2.6/A.3.4/A.4.5) must already be
installed at `org/org_config.json` -- `--org-config` below can do that copy
for you, or do it by hand (or via PrivacyFence Settings' "Install/Update
Organization Config...").

    .venv/bin/python scripts/qa_authenticate_connectors.py
        Runs every Phase A connector's OAuth flow, in the order Phase A
        lists them: Gmail, Drive, Calendar, Contacts, Tasks, Apps Script,
        Slack, Atlassian (Jira + Confluence share one flow), Salesforce.

    .venv/bin/python scripts/qa_authenticate_connectors.py --only slack salesforce
        Runs just the named services/connectors. Accepts either a group
        name (google, slack, atlassian, salesforce) or an individual
        connector (gmail, drive, calendar, contacts, tasks, apps_script) --
        useful for re-running a single step that failed or a single
        account that wasn't ready yet.

    .venv/bin/python scripts/qa_authenticate_connectors.py --org-config org_config.qa.json
        Copies org_config.qa.json into place as org/org_config.json first
        (backing up any file already there), then runs every step.

Stops at the first failed step by default -- a later connector's OAuth
consent screen is more useful to see once the flows before it are known to
work. Pass --continue-on-error to run every requested step regardless and
get one combined summary at the end.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = str(REPO_ROOT / "config" / "settings.yaml")
DEFAULT_ORG_CONFIG_PATH = REPO_ROOT / "org" / "org_config.json"


@dataclass(frozen=True)
class OAuthStep:
    connector: str  # matches daemon_main.TOKEN_FILES's keys
    group: str  # A.1-A.4's service grouping -- what --only also accepts
    flag: str  # daemon_main's --<flag>-oauth CLI argument
    label: str  # printed to the user


# Phase A order: A.1 (Google, six connectors), A.2 (Slack), A.3 (Atlassian --
# one flow activates both Jira and Confluence), A.4 (Salesforce).
STEPS: list[OAuthStep] = [
    OAuthStep("gmail", "google", "--gmail-oauth", "Gmail"),
    OAuthStep("drive", "google", "--drive-oauth", "Drive"),
    OAuthStep("calendar", "google", "--calendar-oauth", "Calendar"),
    OAuthStep("contacts", "google", "--contacts-oauth", "Contacts"),
    OAuthStep("tasks", "google", "--tasks-oauth", "Tasks"),
    OAuthStep("apps_script", "google", "--apps-script-oauth", "Apps Script"),
    OAuthStep("slack", "slack", "--slack-oauth", "Slack"),
    OAuthStep("atlassian", "atlassian", "--atlassian-oauth", "Atlassian (Jira + Confluence)"),
    OAuthStep("salesforce", "salesforce", "--salesforce-oauth", "Salesforce"),
]

GROUPS = {step.group for step in STEPS}
CONNECTOR_NAMES = {step.connector for step in STEPS}


def resolve_steps(only: list[str] | None) -> list[OAuthStep]:
    """Expands --only's group/connector names into an ordered step list,
    preserving Phase A's own ordering regardless of the order --only names
    were given in. Raises ValueError on an unknown name so a typo fails
    loudly rather than silently running nothing for it."""
    if not only:
        return list(STEPS)
    wanted = set(only)
    unknown = wanted - GROUPS - CONNECTOR_NAMES
    if unknown:
        valid = sorted(GROUPS | CONNECTOR_NAMES)
        raise ValueError(f"Unknown --only value(s): {sorted(unknown)}. Valid: {valid}")
    return [step for step in STEPS if step.group in wanted or step.connector in wanted]


def install_org_config(source: Path) -> None:
    """Copies `source` into place as org/org_config.json (what
    daemon_main.load_org_config() always reads -- see paths.org_dir()),
    backing up any file already there rather than silently overwriting it.
    """
    if not source.is_file():
        raise FileNotFoundError(f"--org-config path does not exist: {source}")
    DEFAULT_ORG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEFAULT_ORG_CONFIG_PATH.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = DEFAULT_ORG_CONFIG_PATH.with_name(f"org_config.json.bak.{stamp}")
        shutil.copy2(DEFAULT_ORG_CONFIG_PATH, backup)
        print(f"Existing {DEFAULT_ORG_CONFIG_PATH} backed up to {backup}")
    shutil.copy2(source, DEFAULT_ORG_CONFIG_PATH)
    print(f"Installed {source} as {DEFAULT_ORG_CONFIG_PATH}")


def run_step(step: OAuthStep, config_path: str) -> bool:
    print(f"\n=== {step.label} ({step.flag}) ===")
    print("A browser tab will open -- sign in with the Phase A test account and approve.")
    result = subprocess.run(
        [sys.executable, "-m", "privacyfence.daemon_main", "--config", config_path, step.flag],
        cwd=REPO_ROOT,
    )
    ok = result.returncode == 0
    print(f"{'OK' if ok else 'FAILED'}: {step.label}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Passed through to privacyfence-app. Default: {DEFAULT_CONFIG}")
    parser.add_argument("--org-config", metavar="PATH", help="Install this bundle as org/org_config.json before authenticating.")
    parser.add_argument(
        "--only", nargs="+", metavar="NAME",
        help=f"Limit to these groups/connectors instead of all of Phase A. Groups: {sorted(GROUPS)}. "
             f"Individual connectors: {sorted(CONNECTOR_NAMES)}.",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Run every requested step even after one fails.")
    args = parser.parse_args(argv)

    try:
        steps = resolve_steps(args.only)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.org_config:
        try:
            install_org_config(Path(args.org_config))
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    elif not DEFAULT_ORG_CONFIG_PATH.exists():
        print(
            f"Warning: {DEFAULT_ORG_CONFIG_PATH} not found. Each step below will likely fail its "
            "provider-config lookup. Pass --org-config, or install one first -- see "
            "docs/connector-live-check-setup.md Phase A.1.7/A.2.6/A.3.4/A.4.5.",
            file=sys.stderr,
        )

    results: list[tuple[OAuthStep, bool]] = []
    for step in steps:
        ok = run_step(step, args.config)
        results.append((step, ok))
        if not ok and not args.continue_on_error:
            break

    print("\n=== Summary ===")
    for step, ok in results:
        print(f"  [{'x' if ok else ' '}] {step.label}")
    ran = {step.connector for step, _ in results}
    skipped = [step.label for step in steps if step.connector not in ran]
    for label in skipped:
        print(f"  [ ] {label} (not attempted -- stopped after an earlier failure)")

    all_ok = all(ok for _, ok in results) and not skipped
    if all_ok:
        print(
            "\nAll requested connectors authenticated. Next: "
            ".venv/bin/python scripts/qa_fixture_recorder.py --check "
            "(Phase A.5 exit criteria, connector-live-check-setup.md)."
        )
    else:
        print("\nOne or more steps did not complete. Re-run with --only to retry just those.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
