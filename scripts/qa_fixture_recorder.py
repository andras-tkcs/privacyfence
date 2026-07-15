#!/usr/bin/env python3
"""Local-only QA connector smoke-check and fixture recorder.

Run this on a developer's own machine only -- never in CI, never with any
credential provisioned to GitHub Actions or any other cloud service. It
reuses the exact OAuth token files ``privacyfence-app --<connector>-oauth``
already writes to the git-ignored ``credentials/`` directory
(``daemon_main.TOKEN_FILES``), and talks to your real, already-authenticated
accounts -- the same ones set up per ``docs/qa-environment-setup.md``.

Two modes:

    qa_fixture_recorder.py --check [connector ...]
        Calls each connector's read methods against its tagged seed
        artifact, asserts non-empty/expected results, prints a report.
        Never writes a fixture file. Safe to run any time, as often as
        you like.

    qa_fixture_recorder.py --record [connector ...]
        Does the same targeted calls, redacts real account-identity
        fields (author email, account id, display name -- see
        ``docs/external-api-contract-testing.md``), and writes the result
        to tests/fixtures/live/<connector>/<method>.json.

Both modes accept ``--report-file PATH`` to also save the printed report,
ready to paste into a PR description (see
``docs/external-api-contract-testing.md#local-checks-before-opening-a-pr``).

Only reads from ``tests/fixtures/qa_environment.yaml`` -- a small, non-secret
manifest of your seed artifacts' IDs/keys/tags (see
``docs/qa-environment-setup.md``) -- ever decide *which* real object this
script is allowed to touch. If a fetched object's title/summary doesn't
carry the ``[QATEST]`` tag that manifest expects, recording is refused for
that item rather than silently capturing whatever was fetched.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from privacyfence import daemon_main  # noqa: E402
from privacyfence.confluence_client import ConfluenceClient, ConfluenceClientError  # noqa: E402
from privacyfence.jira_client import JiraClient, JiraClientError  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "live"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "qa_environment.yaml"

QATEST_TAG = "[QATEST]"

# ---------------------------------------------------------------------------- #
# Identity-field redaction -- runs unconditionally on every recording, never
# optional. Content being synthetic (docs/qa-environment-setup.md) does not
# make an API response's structural identity fields synthetic too: a page
# you wrote yourself still says *you* wrote it, in your real account id and
# real name, regardless of what the page says. See
# docs/external-api-contract-testing.md's "Identity-field redaction".
# ---------------------------------------------------------------------------- #

_REDACT_ACCOUNT_ID_KEYS = {"authorid", "accountid", "author_id", "account_id", "createdby", "updatedby", "ownerid"}
_REDACT_EMAIL_KEYS = {"email", "emailaddress", "authoremail", "accountemail"}
# Deliberately narrow -- a bare "name" key is legitimate, non-identity
# content just as often as it's a person's name (a space's name, a page
# title field, a Salesforce record's Name field), so it's excluded here on
# purpose. Only qualified, unambiguously-identity name fields are redacted.
_REDACT_NAME_KEYS = {"displayname", "publicname", "authorname", "accountname"}

_REDACTED_ACCOUNT_ID = "qa-placeholder-account-id"
_REDACTED_EMAIL = "qa-placeholder@example.com"
_REDACTED_NAME = "QA Placeholder"


def redact(value: Any) -> Any:
    """Recursively replace identity-carrying field values with fixed,
    non-empty placeholders (never blank strings -- a blank would be
    indistinguishable from the exact bug assert_no_placeholder_fields
    exists to catch)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_l = k.lower()
            if key_l in _REDACT_ACCOUNT_ID_KEYS:
                out[k] = _REDACTED_ACCOUNT_ID
            elif key_l in _REDACT_EMAIL_KEYS:
                out[k] = _REDACTED_EMAIL
            elif key_l in _REDACT_NAME_KEYS:
                out[k] = _REDACTED_NAME
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# ---------------------------------------------------------------------------- #
# Report
# ---------------------------------------------------------------------------- #

class CheckResult:
    def __init__(
        self, connector: str, method: str, seed_artifact: str, ok: bool, note: str,
        raw: Any = None, fixture_relpath: str = "",
    ) -> None:
        self.connector = connector
        self.method = method
        self.seed_artifact = seed_artifact
        self.ok = ok
        self.note = note
        self.raw = raw
        self.fixture_relpath = fixture_relpath


def _fixture_freshness_lines(connectors: list[str]) -> list[str]:
    lines = []
    for connector in connectors:
        conn_dir = FIXTURES_DIR / connector
        if not conn_dir.exists() or not any(conn_dir.glob("*.json")):
            lines.append(f"Fixture freshness: tests/fixtures/live/{connector}/ has no recorded fixtures yet.")
            continue
        newest = max((f.stat().st_mtime for f in conn_dir.glob("*.json")), default=0)
        age_days = (datetime.datetime.now().timestamp() - newest) / 86400
        lines.append(
            f"Fixture freshness: tests/fixtures/live/{connector}/*.json last recorded "
            f"{datetime.datetime.fromtimestamp(newest):%Y-%m-%d} ({age_days:.0f} days ago)."
        )
    return lines


def render_report(command: str, results: list[CheckResult]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    lines = [f"## PrivacyFence local QA check — {now}", "", f"Command: `{command}`", ""]
    lines.append("| Connector | Method | Seed artifact | Result | Notes |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        mark = "✅ pass" if r.ok else "❌ fail"
        lines.append(f"| {r.connector} | {r.method} | {r.seed_artifact} | {mark} | {r.note} |")
    lines.append("")
    connectors = sorted({r.connector for r in results})
    lines.extend(_fixture_freshness_lines(connectors))
    return "\n".join(lines)


# ---------------------------------------------------------------------------- #
# Manifest
# ---------------------------------------------------------------------------- #

def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        print(
            f"error: manifest not found at {MANIFEST_PATH}\n"
            "Work through docs/qa-environment-setup.md first, then fill in your seed artifacts'\n"
            "IDs/keys in that file.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------- #
# Raw-response capture -- lets --record dump the response exactly as the
# provider returned it, before ConfluenceClient._parse_page_v2 (etc.) ever
# touches it, without this script needing to know any endpoint path itself.
# Works for any client that funnels every call through a single
# ``self._request(fn, *a, **kw)`` choke point, as ConfluenceClient and
# JiraClient both do.
# ---------------------------------------------------------------------------- #

class RawCapture:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.captured: Any = None

    def __enter__(self) -> "RawCapture":
        self._original = self._client._request

        def wrapped(fn: Callable, *args: Any, **kwargs: Any) -> Any:
            result = self._original(fn, *args, **kwargs)
            self.captured = copy.deepcopy(result)
            return result

        self._client._request = wrapped
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client._request = self._original


# ---------------------------------------------------------------------------- #
# Atlassian (Jira + Confluence) -- one OAuth grant, one token file, shared by
# both clients exactly as daemon_main.py shares it. Both funnel every call
# through a single self._request(fn, *a, **kw) choke point, which is what
# RawCapture relies on -- a connector without that shape (most of the rest;
# see CONNECTOR_CHECKS below) needs its own capture mechanism, not just a
# new check_<connector>() function.
# ---------------------------------------------------------------------------- #

def _load_atlassian_config() -> tuple[dict[str, Any], str]:
    org_config = daemon_main.load_org_config()
    atlassian_org = org_config.get("atlassian") or {}
    if not atlassian_org.get("client_id"):
        raise SystemExit("Atlassian organization config not installed -- run Authenticate… in the menu bar first.")
    token_path = daemon_main._resolve_path(daemon_main.TOKEN_FILES["atlassian"])
    token = daemon_main.load_atlassian_token(token_path)
    return {**atlassian_org, **(token or {})}, token_path


def _build_confluence_client() -> ConfluenceClient:
    config, token_path = _load_atlassian_config()
    return ConfluenceClient(config=config, token_file=token_path)


def _build_jira_client() -> JiraClient:
    config, token_path = _load_atlassian_config()
    return JiraClient(config=config, token_file=token_path)


def check_confluence(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("confluence") or {}
    space_key = cfg.get("space_key", "PFQA")
    seed_page_id = cfg.get("seed_page_id", "")
    seed_page_title = cfg.get("seed_page_title", f"PrivacyFence QA seed page {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_confluence_client()

    # list_spaces -- only ever recorded filtered to the one QA space, never
    # a full real list of every space on the site.
    try:
        with RawCapture(client) as cap:
            spaces = client.list_spaces(max_results=250)
        match = next((s for s in spaces if s.key == space_key), None)
        ok = match is not None
        note = "found" if ok else f"space key {space_key!r} not in list_spaces() result"
        raw = None
        if record and ok and isinstance(cap.captured, dict):
            filtered = dict(cap.captured)
            filtered["results"] = [r for r in (cap.captured.get("results") or []) if r.get("key") == space_key]
            raw = redact(filtered)
        results.append(CheckResult("confluence", "list_spaces", space_key, ok, note, raw, "list_spaces.json"))
    except ConfluenceClientError as exc:
        results.append(CheckResult("confluence", "list_spaces", space_key, False, str(exc)))

    # get_page -- targeted at the seed page's id if the manifest has it,
    # otherwise resolved once by title.
    try:
        with RawCapture(client) as cap:
            page = client.get_page(seed_page_id) if seed_page_id else client.get_page_by_title(space_key, seed_page_title)
        tagged = QATEST_TAG in page.title
        complete = bool(page.title and page.author and page.updated and page.space_key)
        ok = tagged and complete
        if not tagged:
            note = f"fetched page title {page.title!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- title/author/updated/space_key not all present"
        else:
            note = "title, author, updated, space_key all present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("confluence", "get_page", seed_page_title, ok, note, raw, "get_page.json"))
    except ConfluenceClientError as exc:
        results.append(CheckResult("confluence", "get_page", seed_page_title, False, str(exc)))

    return results


def check_jira(record: bool, manifest: dict[str, Any]) -> list[CheckResult]:
    cfg = manifest.get("jira") or {}
    project_key = cfg.get("project_key", "PFQA")
    seed_issue_key = cfg.get("seed_issue_key", "")
    seed_issue_summary = cfg.get("seed_issue_summary", f"PrivacyFence QA seed issue {QATEST_TAG}")

    results: list[CheckResult] = []
    client = _build_jira_client()

    # list_projects -- JiraClient.list_projects() returns a plain list, not
    # Confluence's {"results": [...]} envelope; only ever recorded filtered
    # to the one QA project, never every real project on the site.
    try:
        with RawCapture(client) as cap:
            projects = client.list_projects(max_results=250)
        match = next((p for p in projects if p.key == project_key), None)
        ok = match is not None
        note = "found" if ok else f"project key {project_key!r} not in list_projects() result"
        raw = None
        if record and ok and isinstance(cap.captured, list):
            raw = redact([p for p in cap.captured if p.get("key") == project_key])
        results.append(CheckResult("jira", "list_projects", project_key, ok, note, raw, "list_projects.json"))
    except JiraClientError as exc:
        results.append(CheckResult("jira", "list_projects", project_key, False, str(exc)))

    # get_issue -- targeted at the seed issue's key if the manifest has it,
    # otherwise resolved once via a JQL search scoped to project + summary.
    try:
        if seed_issue_key:
            with RawCapture(client) as cap:
                issue = client.get_issue(seed_issue_key)
        else:
            found = client.search_issues(
                f'project = {project_key} AND summary ~ "{seed_issue_summary}"', max_results=1,
            )
            if not found:
                raise JiraClientError(
                    f"no issue found matching summary {seed_issue_summary!r} in {project_key!r}"
                )
            with RawCapture(client) as cap:
                issue = client.get_issue(found[0].key)
        tagged = QATEST_TAG in issue.summary
        # assignee is legitimately "(unassigned)"/empty on a fresh seed issue
        # -- not part of the completeness check, unlike Confluence's author,
        # which the client always populates from a real accountId.
        complete = bool(issue.key and issue.summary and issue.status)
        ok = tagged and complete
        if not tagged:
            note = f"fetched issue summary {issue.summary!r} does not carry {QATEST_TAG} -- refusing to record"
        elif not complete:
            note = "missing popup field(s) -- key/summary/status not all present"
        else:
            note = "key, summary, status all present"
        raw = redact(cap.captured) if (record and ok and isinstance(cap.captured, dict)) else None
        results.append(CheckResult("jira", "get_issue", seed_issue_summary, ok, note, raw, "get_issue.json"))
    except JiraClientError as exc:
        results.append(CheckResult("jira", "get_issue", seed_issue_summary, False, str(exc)))

    return results


CONNECTOR_CHECKS: dict[str, Callable[[bool, dict[str, Any]], list[CheckResult]]] = {
    "confluence": check_confluence,
    "jira": check_jira,
}


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #

def run(mode: str, connectors: list[str], report_file: str | None) -> int:
    manifest = load_manifest()
    requested = connectors or sorted(CONNECTOR_CHECKS)
    record = mode == "record"

    all_results: list[CheckResult] = []
    for name in requested:
        check_fn = CONNECTOR_CHECKS.get(name)
        if check_fn is None:
            print(f"'{name}' has no recorder implementation yet -- see scripts/qa_fixture_recorder.py "
                  "CONNECTOR_CHECKS and docs/external-api-contract-testing.md's Part A.", file=sys.stderr)
            continue
        all_results.extend(check_fn(record, manifest))

    if record:
        for r in all_results:
            if r.raw is None:
                continue
            out_dir = FIXTURES_DIR / r.connector
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / r.fixture_relpath).write_text(json.dumps(r.raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    command = f"qa_fixture_recorder.py --{mode} {' '.join(requested)}"
    report = render_report(command, all_results)
    print(report)
    if report_file:
        Path(report_file).write_text(report + "\n", encoding="utf-8")

    return 0 if all(r.ok for r in all_results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--check", action="store_true", help="Smoke-check only; never writes a fixture.")
    mode_group.add_argument("--record", action="store_true", help="Check and (re-)record fixtures.")
    parser.add_argument("connectors", nargs="*", help="Connector name(s), e.g. confluence. Default: all implemented.")
    parser.add_argument("--report-file", help="Also save the printed report to this path.")
    args = parser.parse_args()

    mode = "record" if args.record else "check"
    return run(mode, args.connectors, args.report_file)


if __name__ == "__main__":
    raise SystemExit(main())
